"""Port of apps.scent-library.fragrance-preview.jsx's loader (GET) and action (POST). Same App
Proxy path, same recommendation lookup, same recreate/save_build/add_to_cart behavior -- only the
rendering technology changed (Jinja2 + vanilla JS instead of React), per the explicit decision to
migrate, not redesign, this page.

Phase 1 (security, F1 / F2 / N1 / N3):
  * the App Proxy signature proves Shopify proxied the request; `verified_shop` additionally
    proves the shop is OUR configured store;
  * a recommendation id is not authorization. Both GET and POST require the build capability
    token (`bt` query parameter on GET, `buildToken` in the POST body) minted for that exact
    recommendation when the backend emitted preview_ready. GET is gated too, deliberately: the
    page embeds the customer's draft name, pricing, the Shopify product id, and the token itself;
  * ratios and name go through the shared validator before anything is persisted or sent to
    Shopify; the Shopify product id always comes from the recommendation row;
  * nothing is written (not even the draft) until the caller is authorized.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.services.build_capability import BUILD_TOKEN_QUERY_PARAM, BuildNotAuthorized, authorize_build_token, bind_build_capability_customer
from app.services.customer_identity import VerifiedShopifyCustomer, verified_shopify_customer_from_signed_params
from app.services.conversation import save_message
from app.services.customer_profile import get_customer_profile, save_customer_profile_field
from app.services.data_lifecycle import ConversationDeleted
from app.services.turn_lock import TurnInProgress, conversation_turn_lock
from app.services.fragrance_build import compute_default_ratios, compute_note_position_buckets, compute_price_per_5ml_by_position
from app.services.build_commerce import BuildOperationInProgress, BuildPendingReview, execute_build_commerce, save_recreate_draft
from app.services.commerce_inventory import InventoryNotVerified, commerce_failure
from app.services.recommendation_confirmation import get_recommendation
from app.shopify.admin_auth import get_admin_access_token
from app.shopify.admin_client import ShopNotAuthenticated
from app.shopify.app_proxy import verified_signed_params
from app.shopify.build_input import InvalidCustomName, InvalidRatios, validate_custom_name, validate_ratios
from app.shopify.builds import BuildProductNotSaved, BuildWriteAmbiguous, InvalidComputedPrice, ProductPricingNotFound
from app.shopify.trusted_shop import UntrustedShopError

logger = logging.getLogger(__name__)

RECREATE_REENTRY_MESSAGE = "What would you like to change about your fragrance?"

# Customer-safe -- never mentions tokens, sessions, or internal auth mechanics. Used whenever this
# shop has no usable Shopify Admin credentials, whether that's a missing Session row or a token
# Shopify itself rejected (401/403) on the actual request.
_NOT_CONNECTED_MESSAGE = "This store isn't connected to Shopify for building products right now — an admin needs to reconnect the app before builds can be saved."
_NOT_AUTHORIZED_MESSAGE = "This fragrance preview link is not valid or has expired. Please reopen it from the chat."

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# The preview page and its actions are personalized and carry the build capability: never cache,
# never leak the URL (which carries `bt`) in a Referer.
_SENSITIVE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


async def _authorize_preview(session: AsyncSession, *, token: object, recommendation_id: object, customer: VerifiedShopifyCustomer | None):
    """Build capability first; then, if Shopify signed a logged-in customer, bind or check it.
    A capability bound to customer A refuses customer B even with the token."""
    capability = await authorize_build_token(session, token=token, recommendation_id=recommendation_id, verified_shopify_customer_id=customer.customer_id if customer else None)
    if customer and not capability.verifiedShopifyCustomerId:
        await bind_build_capability_customer(session, capability, customer.customer_id)
    return capability

# Shopify's numeric REST-style ID from a GraphQL GID (e.g. "gid://shopify/ProductVariant/123" ->
# "123") -- needed for the cart permalink URL format (/cart/{variantId}:{quantity}).
_NUMERIC_ID_PATTERN = re.compile(r"(\d+)$")


def _json(content: dict) -> JSONResponse:
    return JSONResponse(content=content, headers=_SENSITIVE_HEADERS)


def _numeric_id_from_gid(gid: str | None) -> str | None:
    match = _NUMERIC_ID_PATTERN.search(str(gid or ""))
    return match.group(1) if match else None


def _safe_json_for_script_tag(data: dict[str, Any]) -> str:
    # Prevents a "</script>" inside any string field (a product title, a note) from prematurely
    # closing the <script> tag this gets embedded in -- a standard, safe JSON-in-HTML technique.
    return json.dumps(data).replace("</", "<\\/")


@router.get("/apps/scent-library/fragrance-preview", response_class=HTMLResponse)
async def preview_page(request: Request, signed: dict = Depends(verified_signed_params), session: AsyncSession = Depends(get_session)):
    recommendation_id = request.query_params.get("recommendationId")
    if not recommendation_id:
        raise HTTPException(status_code=400, detail="recommendationId is required")

    # AUTHORIZE before revealing anything about the recommendation (including whether it exists).
    build_token = request.query_params.get(BUILD_TOKEN_QUERY_PARAM)
    try:
        await _authorize_preview(session, token=build_token, recommendation_id=recommendation_id, customer=verified_shopify_customer_from_signed_params(signed))
    except BuildNotAuthorized:
        raise HTTPException(status_code=403, detail=_NOT_AUTHORIZED_MESSAGE, headers=_SENSITIVE_HEADERS) from None

    recommendation = await get_recommendation(session, recommendation_id)
    if not recommendation:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    internal_products = recommendation.productsJson if isinstance(recommendation.productsJson, list) else []
    customer_likes = (recommendation.customerProfileJson or {}).get("likes") or []
    buckets = compute_note_position_buckets(internal_products, customer_likes)
    ratios = recommendation.draftRatiosJson or compute_default_ratios(buckets)
    customer_facing_name = (recommendation.customerFacingJson or {}).get("customerFacingName") or "Custom Blend"

    ratios_by_product = recommendation.ratiosJson if isinstance(recommendation.ratiosJson, list) else []
    price_per_5ml_by_position = await compute_price_per_5ml_by_position(session, internal_products, ratios_by_product)

    profile_pills = list(dict.fromkeys([*buckets["top"], *buckets["middle"], *buckets["base"]]))[:8]

    data = {
        "recommendationId": recommendation_id,
        # The capability the page must present on every POST. Same-origin page data, never logged.
        "buildToken": build_token,
        "name": recommendation.draftName or customer_facing_name,
        "buckets": buckets,
        "ratios": ratios,
        "pricePer5mlByPosition": price_per_5ml_by_position,
        "profilePills": profile_pills,
        # Phase 3: nothing else. No product titles (internal evidence), no Shopify ids, no
        # build status, no per-component structure -- the page's JS uses none of them.
    }

    return templates.TemplateResponse(
        request, "fragrance_preview.html",
        {"data": data, "data_json": _safe_json_for_script_tag(data)},
        headers=_SENSITIVE_HEADERS,
    )


class PreviewAction(BaseModel):
    intent: str
    recommendationId: str
    buildToken: str | None = None
    name: Any = None
    # Validated by app/shopify/build_input.py, not by pydantic's lenient float coercion.
    ratios: Any = None


@router.post("/apps/scent-library/fragrance-preview")
async def preview_action(body: PreviewAction, signed: dict = Depends(verified_signed_params), session: AsyncSession = Depends(get_session)) -> JSONResponse:
    shop = signed["shop"]
    if body.intent not in ("recreate", "save_build", "add_to_cart"):
        raise HTTPException(status_code=400, detail=f'Unknown intent "{body.intent}".')

    # ---- AUTHORIZE first: no draft write, no lookup result, no Shopify call before this ----
    try:
        await _authorize_preview(session, token=body.buildToken, recommendation_id=body.recommendationId, customer=verified_shopify_customer_from_signed_params(signed))
    except BuildNotAuthorized:
        logger.info("PREVIEW_ACTION_REJECTED %s", json.dumps({"shop": shop, "recommendationId": body.recommendationId, "intent": body.intent, "reason": "build_not_authorized"}))
        return _json({"error": _NOT_AUTHORIZED_MESSAGE, "code": "build_not_authorized"})

    recommendation = await get_recommendation(session, body.recommendationId)
    if not recommendation:
        return _json({"error": "Recommendation not found."})

    # ---- VALIDATE INPUT (shared rules for every intent) ----
    try:
        name = validate_custom_name(body.name)
        ratios = validate_ratios(body.ratios) if (body.ratios is not None or body.intent != "recreate") else None
    except (InvalidRatios, InvalidCustomName) as err:
        return _json({"error": str(err), "code": "invalid_input"})

    if body.intent == "recreate":
        # Phase 6A: the conversation writes below happen under the conversation turn lock (class 1),
        # taken BEFORE the build lock (class 2), the same order deletion uses. Each write also
        # applies rule W, so a deleted conversation is refused whatever the caller cached.
        try:
            async with conversation_turn_lock(recommendation.conversationId):
                await save_recreate_draft(session, recommendation_id=body.recommendationId, name=name, ratios=ratios)
                # N14: the re-entry prompt is appended HERE, by this explicit authorized POST.
                await save_message(session, recommendation.conversationId, "assistant", RECREATE_REENTRY_MESSAGE)
                await save_customer_profile_field(session, recommendation.conversationId, "pendingRecreateRecommendationId", body.recommendationId)
        except (BuildOperationInProgress, TurnInProgress):
            return _json(commerce_failure("build_in_progress")[1])
        except BuildPendingReview:
            return _json(commerce_failure("build_pending_review")[1])
        except ConversationDeleted:
            return _json({"error": _NOT_AUTHORIZED_MESSAGE, "code": "build_not_authorized"})
        return _json({"status": "recreate", "redirectUrl": f"https://{shop}/"})

    log_prefix = "SAVE_BUILD" if body.intent == "save_build" else "ADD_TO_CART"
    logger.info("%s_STARTED %s", log_prefix, json.dumps({"shop": shop, "recommendationId": body.recommendationId}))

    # Phase 5A: the draft is shared build state; it is saved INSIDE the per-recommendation lock by
    # execute_build_commerce(save_draft=True), never before it.


    # Fail fast with a clear, customer-safe reason instead of letting every downstream GraphQL
    # call fail one by one. get_admin_access_token only ever operates on the trusted shop and
    # never logs the token itself.
    try:
        token, auth_source = await get_admin_access_token(session, shop)
    except UntrustedShopError:
        logger.error("SHOPIFY_SESSION_MISSING %s", json.dumps({"shop": shop, "recommendationId": body.recommendationId, "reason": "untrusted_shop"}))
        return _json({"error": _NOT_CONNECTED_MESSAGE})
    logger.info("SHOPIFY_SESSION_LOOKUP %s", json.dumps({"shop": shop, "hasToken": bool(token), "source": auth_source}))
    if not token:
        logger.error("SHOPIFY_SESSION_MISSING %s", json.dumps({"shop": shop, "recommendationId": body.recommendationId}))
        return _json({"error": _NOT_CONNECTED_MESSAGE})

    # Phase 5 (F9): one orchestration for every controlled commerce action. It serializes
    # operations per recommendation, re-reads the build inside the lock, and the Shopify write
    # layer it calls verifies inventory immediately before its first write. Nothing below reports
    # success unless the operation really completed.
    try:
        identity_profile = await get_customer_profile(session, recommendation.conversationId)
        outcome = await execute_build_commerce(
            session, shop, recommendation_id=body.recommendationId, ratios=ratios, name=name,
            customer_name=identity_profile.get("name"), customer_email=identity_profile.get("email"), save_draft=True,
        )
        shopify_product_id, shopify_variant_id, product_url = outcome["productId"], outcome["variantId"], outcome["productUrl"]
        logger.info("SHOPIFY_VARIANT_RESOLVED %s", json.dumps({"shop": shop, "recommendationId": body.recommendationId, "created": outcome["created"]}))
    except ConversationDeleted:
        return _json({"error": _NOT_AUTHORIZED_MESSAGE, "code": "build_not_authorized"})
    except InventoryNotVerified as err:
        logger.info("COMMERCE_BLOCKED %s", json.dumps({"shop": shop, "recommendationId": body.recommendationId, "intent": body.intent, "state": err.state.value}))
        return _json(commerce_failure(err.state)[1])
    except BuildOperationInProgress:
        return _json(commerce_failure("build_in_progress")[1])
    except (BuildPendingReview, BuildWriteAmbiguous):
        return _json(commerce_failure("build_pending_review")[1])
    except ShopNotAuthenticated:
        # Session row disappeared between the pre-check above and the actual call (e.g.
        # APP_UNINSTALLED fired mid-request) -- same customer-safe framing either way.
        logger.error("SHOPIFY_PRODUCT_CREATE_FAILED %s", json.dumps({"shop": shop, "recommendationId": body.recommendationId, "reason": "not_authenticated"}))
        return _json({"error": _NOT_CONNECTED_MESSAGE})
    except UntrustedShopError:
        logger.error("SHOPIFY_PRODUCT_CREATE_FAILED %s", json.dumps({"shop": shop, "recommendationId": body.recommendationId, "reason": "untrusted_shop"}))
        return _json({"error": _NOT_CONNECTED_MESSAGE})
    except httpx.HTTPStatusError as err:
        # The real, previously-swallowed failure mode: a stored token that Shopify itself
        # rejects (401/403) -- wrong app's token, revoked, or the install was never completed
        # for this app. Never SHOPIFY_API_SECRET-based workaround here; the fix is a real
        # offline token for this app, not a different credential.
        status = err.response.status_code
        logger.error("SHOPIFY_PRODUCT_CREATE_FAILED %s", json.dumps({"shop": shop, "recommendationId": body.recommendationId, "reason": "shopify_http_error", "status": status}))
        if status in (401, 403):
            return _json({"error": _NOT_CONNECTED_MESSAGE})
        return _json({"error": "Shopify couldn't process this build right now — please try again shortly."})
    except (InvalidRatios, InvalidCustomName, InvalidComputedPrice, ProductPricingNotFound, BuildProductNotSaved) as err:
        logger.info("SHOPIFY_PRODUCT_CREATE_REJECTED %s", json.dumps({"shop": shop, "recommendationId": body.recommendationId, "reason": type(err).__name__}))
        return _json({"error": str(err)})
    except Exception as err:
        logger.error("SHOPIFY_PRODUCT_CREATE_FAILED %s", json.dumps({"shop": shop, "recommendationId": body.recommendationId, "reason": "unexpected", "errorType": type(err).__name__}))
        return _json({"error": "Failed to save the build."})

    if body.intent == "save_build":
        logger.info("SAVE_BUILD_COMPLETED %s", json.dumps({"shop": shop, "recommendationId": body.recommendationId, "shopifyProductId": shopify_product_id}))
        return _json({"status": "saved", "shopifyProductId": shopify_product_id, "shopifyVariantId": shopify_variant_id, "productUrl": product_url})

    numeric_variant_id = _numeric_id_from_gid(shopify_variant_id)
    logger.info("ADD_TO_CART_COMPLETED %s", json.dumps({"shop": shop, "recommendationId": body.recommendationId, "shopifyProductId": shopify_product_id}))
    return _json({"status": "added", "shopifyProductId": shopify_product_id, "shopifyVariantId": shopify_variant_id, "cartUrl": f"https://{shop}/cart/{numeric_variant_id}:1"})
