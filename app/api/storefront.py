"""Storefront account features, served ONLY through Shopify's App Proxy (/apps/scent-library/...).

The App Proxy signature is what makes these routes possible: Shopify adds `logged_in_customer_id`
to every proxied request and signs it, so the storefront page can call these same-origin routes and
the server learns WHICH customer is signed in without trusting anything the browser says.

  * POST /account/link  -- binds the browser's conversation (proved by its conversation token) to
    the signed-in customer, and fills the conversation's EMPTY name/email from the customer's
    Shopify account. Same fill-empty rule the chat already applies; nothing is overwritten and
    no profiling question or readiness rule changes. A conversation already bound to a different
    customer is refused (account switching never reveals another customer's chat).
  * GET  /my-builds     -- the signed-in customer's confirmed builds, paginated. Ownership is a
    verified binding (a conversation or build capability bound to this customer), never an email.
  * POST /my-builds/open -- re-opens one owned build's preview by minting a fresh build capability
    bound to this customer.

A guest (no signed customer) gets `sign_in_required`; nothing is looked up for them.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.preview_url import build_preview_url
from app.api.request_limits import InvalidChatInput, validate_conversation_id, validate_token_shape
from app.config import settings
from app.db.models import BuildCapability, ConversationCapability, FragranceRecommendation
from app.db.session import get_session
from app.services.build_capability import bind_build_capability_customer, hash_build_token, issue_build_token
from app.services.build_commerce import BUILD_STATUS_CREATING, BUILD_STATUS_PENDING_REVIEW
from app.services.conversation import create_or_update_conversation
from app.services.conversation_capability import ConversationNotAuthorized, authorize_conversation, bind_verified_customer
from app.services.customer_identity import clean_self_reported_email, clean_self_reported_name, verified_shopify_customer_from_signed_params
from app.services.customer_profile import get_customer_profile, save_customer_profile_fields
from app.services.data_lifecycle import ConversationDeleted, is_detached
from app.services.rate_limit import RateLimitUnavailable, RateLimited, enforce, limit
from app.services.turn_lock import TurnInProgress, conversation_turn_lock
from app.shopify.admin_client import admin_graphql
from app.shopify.app_proxy import verified_signed_params
from app.shopify.customers import fetch_customer_contact

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/apps/scent-library")

_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
_PAGE_SIZE = 12
_SIGN_IN_REQUIRED = {"error": "Please sign in to your account to see your fragrances.", "code": "sign_in_required"}
_NOT_AUTHORIZED = {"error": "This conversation session is not valid or has expired. Please start a new conversation.", "code": "conversation_not_authorized"}
_NOT_FOUND = {"error": "We couldn't find that fragrance in your account.", "code": "build_not_found"}


def _json(content: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(content, status_code=status, headers=_HEADERS)


async def _limited(session: AsyncSession, limit_class: str, customer_id: str) -> JSONResponse | None:
    # Behind the App Proxy every request comes from Shopify's address, so the limit subject is
    # the Shopify-signed customer, never the network peer.
    try:
        await enforce(session, [limit(limit_class, customer_id, settings.rate_limit_history_read_per_conversation)])
    except RateLimited as err:
        return JSONResponse({"error": "Too many requests. Please wait a moment and try again.", "code": "rate_limited"}, status_code=429, headers={"Retry-After": str(err.retry_after_seconds), **_HEADERS})
    except RateLimitUnavailable:
        return _json({"error": "Service temporarily unavailable.", "code": "unavailable"}, 503)
    return None


# ---------------------------------------------------------------------------
# Account link
# ---------------------------------------------------------------------------

class AccountLinkRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    conversationId: Any = None
    conversationToken: Any = None


@router.post("/account/link")
async def link_account(body: AccountLinkRequest, signed: dict = Depends(verified_signed_params), session: AsyncSession = Depends(get_session)) -> JSONResponse:
    customer = verified_shopify_customer_from_signed_params(signed)
    if customer is None:
        return _json({"linked": False, "signedIn": False})
    if (refused := await _limited(session, "account_link_customer", customer.customer_id)) is not None:
        return refused
    try:
        conversation_id = validate_conversation_id(body.conversationId)
        token = validate_token_shape(body.conversationToken)
        capability = await authorize_conversation(session, token=token, conversation_id=conversation_id, verified_shopify_customer_id=customer.customer_id)
    except (InvalidChatInput, ConversationNotAuthorized):
        logger.info("ACCOUNT_LINK_REFUSED %s", json.dumps({"reason": "conversation_not_authorized"}))
        return _json(_NOT_AUTHORIZED, 401)
    await bind_verified_customer(session, capability, customer.customer_id)

    contact = await fetch_customer_contact(session, signed["shop"], customer.customer_id)
    name = clean_self_reported_name((contact or {}).get("firstName"))
    email = clean_self_reported_email((contact or {}).get("email"))
    try:
        async with conversation_turn_lock(conversation_id):
            # Fill-empty only: the same precedence the chat applies to known account details.
            profile = await get_customer_profile(session, conversation_id)
            fill = {k: v for k, v in (("name", name), ("email", email)) if v and not profile.get(k)}
            if fill:
                profile = await save_customer_profile_fields(session, conversation_id, fill)
            await create_or_update_conversation(session, conversation_id, email, name)
    except TurnInProgress:
        return _json({"linked": True, "signedIn": True, "retry": True, "code": "turn_in_progress"}, 409)
    except ConversationDeleted:
        return _json(_NOT_AUTHORIZED, 401)
    logger.info("ACCOUNT_LINKED %s", json.dumps({
        "conversationId": conversation_id, "contactAvailable": contact is not None,
        "nameFilled": "name" in fill, "emailFilled": "email" in fill,
    }))
    return _json({"linked": True, "signedIn": True, "nameKnown": bool(profile.get("name")), "emailKnown": bool(profile.get("email"))})


# ---------------------------------------------------------------------------
# My Builds
# ---------------------------------------------------------------------------

def _owned_by(customer_id: str):
    """A recommendation is owned when its conversation, or a build capability for it, is bound to
    this Shopify-signed customer. Confirmed builds only (pending/expired never had a preview)."""
    conversations = select(ConversationCapability.conversationId).where(ConversationCapability.verifiedShopifyCustomerId == customer_id)
    builds = select(BuildCapability.recommendationId).where(BuildCapability.verifiedShopifyCustomerId == customer_id)
    return (
        or_(FragranceRecommendation.conversationId.in_(conversations), FragranceRecommendation.id.in_(builds)),
        FragranceRecommendation.status == "confirmed",
    )


def _customer_status(build_status: str) -> str:
    if build_status == "saved":
        return "saved"
    if build_status in (BUILD_STATUS_CREATING, BUILD_STATUS_PENDING_REVIEW):
        return "processing"
    return "draft"


async def _product_urls(session: AsyncSession, shop: str, product_ids: list[str]) -> dict[str, str]:
    """Storefront URLs for ACTIVE products only (a draft-first product that never activated has no
    public page). One batched read; any failure just means no links."""
    if not product_ids:
        return {}
    try:
        payload = await admin_graphql(session, shop, "query myBuildProducts($ids: [ID!]!) { nodes(ids: $ids) { ... on Product { id handle status } } }", {"ids": product_ids})
    except Exception as err:  # noqa: BLE001
        logger.info("MY_BUILDS_PRODUCT_LOOKUP_FAILED %s", json.dumps({"errorType": type(err).__name__}))
        return {}
    urls = {}
    for node in (payload.get("data") or {}).get("nodes") or []:
        if isinstance(node, dict) and node.get("status") == "ACTIVE" and node.get("handle"):
            urls[node["id"]] = f"https://{shop}/products/{node['handle']}"
    return urls


@router.get("/my-builds")
async def my_builds(page: int = Query(default=1, ge=1, le=1000), signed: dict = Depends(verified_signed_params), session: AsyncSession = Depends(get_session)) -> JSONResponse:
    customer = verified_shopify_customer_from_signed_params(signed)
    if customer is None:
        return _json(_SIGN_IN_REQUIRED, 401)
    if (refused := await _limited(session, "my_builds_customer", customer.customer_id)) is not None:
        return refused
    where = _owned_by(customer.customer_id)
    total = await session.scalar(select(func.count(FragranceRecommendation.id)).where(*where))
    rows = [r for r in (await session.scalars(
        select(FragranceRecommendation).where(*where).order_by(FragranceRecommendation.createdAt.desc(), FragranceRecommendation.id).limit(_PAGE_SIZE).offset((page - 1) * _PAGE_SIZE)
    )).all() if not is_detached(r.conversationId)]
    urls = await _product_urls(session, signed["shop"], [r.shopifyProductId for r in rows if r.shopifyProductId and r.buildStatus == "saved"])
    builds = [{
        # Customer-safe fields only: the customer-facing name, never a component or source title.
        "recommendationId": r.id,
        "name": r.draftName or (r.customerFacingJson or {}).get("customerFacingName") or "Custom Blend",
        "createdAt": r.createdAt.isoformat() + "Z",
        "status": _customer_status(r.buildStatus),
        "productUrl": urls.get(r.shopifyProductId or ""),
    } for r in rows]
    logger.info("MY_BUILDS_LISTED %s", json.dumps({"count": len(builds), "page": page}))
    return _json({"builds": builds, "total": total, "page": page, "pageSize": _PAGE_SIZE})


class OpenBuildRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    recommendationId: Any = None


@router.post("/my-builds/open")
async def open_my_build(body: OpenBuildRequest, signed: dict = Depends(verified_signed_params), session: AsyncSession = Depends(get_session)) -> JSONResponse:
    customer = verified_shopify_customer_from_signed_params(signed)
    if customer is None:
        return _json(_SIGN_IN_REQUIRED, 401)
    if (refused := await _limited(session, "my_builds_customer", customer.customer_id)) is not None:
        return refused
    if not isinstance(body.recommendationId, str) or not 0 < len(body.recommendationId) <= 64:
        return _json(_NOT_FOUND, 404)
    record = await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == body.recommendationId, *_owned_by(customer.customer_id)))
    if record is None or is_detached(record.conversationId):
        return _json(_NOT_FOUND, 404)  # identical for "not yours" and "does not exist"
    try:
        token = await issue_build_token(session, recommendation_id=record.id, conversation_id=record.conversationId, shop=signed["shop"])
    except ConversationDeleted:
        return _json(_NOT_FOUND, 404)
    capability = await session.scalar(select(BuildCapability).where(BuildCapability.tokenHash == hash_build_token(token)))
    await bind_build_capability_customer(session, capability, customer.customer_id)
    logger.info("MY_BUILD_REOPENED %s", json.dumps({"recommendationId": record.id}))
    return _json({"previewUrl": build_preview_url(signed["shop"], record.id, token)})
