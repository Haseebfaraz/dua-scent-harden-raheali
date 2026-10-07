"""Merchant admin dashboard: the embedded Shopify admin page and its read-only JSON API.

Restores the old app's customer list / customer detail / documentation pages behind real merchant
authentication (app/shopify/session_token.py), scoped to the one trusted shop.

  * every /admin/api route requires a valid App Bridge session token for the trusted shop;
  * everything here is READ ONLY: no Shopify call, no Odoo call, no retry of anything. Historical
    inventory comes from the stored RecommendationInventorySnapshot rows only;
  * internal components, scores and inventory evidence are returned ONLY here (merchant
    authorized). Nothing in this module is reachable from the storefront, chat or preview;
  * search and pagination run in PostgreSQL; page sizes are bounded.
"""

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db.models import Conversation, CustomerProfileState, FragranceRecommendation, Message, RecommendationInventorySnapshot
from app.db.session import get_session
from app.services.build_commerce import BUILD_STATUS_CREATING, BUILD_STATUS_PENDING_REVIEW
from app.shopify.session_token import MerchantSession, SessionTokenInvalid, verify_session_token
from app.shopify.trusted_shop import UntrustedShopError, trusted_shop

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

_NO_STORE = {"Cache-Control": "no-store"}
MAX_PAGE_SIZE = 50
ATTENTION_STATUSES = (BUILD_STATUS_CREATING, BUILD_STATUS_PENDING_REVIEW)
# The profile fields a merchant sees. Internal flow markers (pending ids, asked-flags) are omitted.
PROFILE_FIELDS = (
    "name", "email", "city", "stateRegion", "country", "likes", "dislikes", "preferredStyle", "inferredStyle",
    "occasion", "giftRecipient", "strengthPreference", "requestedSeasonStyle", "weatherDirection", "locationVerified",
    "additionalPreferences",
)


def require_merchant(request: Request) -> MerchantSession:
    auth = request.headers.get("authorization") or ""
    token = auth[7:] if auth[:7].lower() == "bearer " else None
    try:
        merchant = verify_session_token(token)
    except SessionTokenInvalid:
        logger.info("ADMIN_ACCESS_DENIED %s", json.dumps({"reason": "invalid_session_token"}))
        raise HTTPException(status_code=401, detail="merchant session required", headers=_NO_STORE) from None
    route = request.scope.get("route")
    logger.info("ADMIN_ACCESS %s", json.dumps({"userId": merchant.user_id, "route": getattr(route, "path", None)}))
    return merchant


def _iso(value) -> str | None:
    return value.isoformat() + "Z" if value is not None else None


def _page(page: int, page_size: int) -> tuple[int, int]:
    return max(page, 1), min(max(page_size, 1), MAX_PAGE_SIZE)


def _admin_product_url(product_gid: str | None) -> str | None:
    if not product_gid:
        return None
    numeric = product_gid.rsplit("/", 1)[-1]
    try:
        return f"https://{trusted_shop()}/admin/products/{numeric}" if numeric.isdigit() else None
    except UntrustedShopError:
        return None


# ---------------------------------------------------------------------------
# Embedded shell
# ---------------------------------------------------------------------------

@router.get("/admin", response_class=HTMLResponse)
async def admin_shell(request: Request):
    """The page itself carries no customer data (every datum comes from /admin/api with a session
    token), so it is served without auth. It may only be framed by the Shopify admin."""
    try:
        frame_ancestors = f"https://{trusted_shop()} https://admin.shopify.com"
    except UntrustedShopError:
        frame_ancestors = "'none'"
    return templates.TemplateResponse(
        request, "admin.html", {"api_key": settings.shopify_api_key},
        headers={**_NO_STORE, "Content-Security-Policy": f"frame-ancestors {frame_ancestors};", "Referrer-Policy": "no-referrer"},
    )


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------

_LIST_SQL = """
SELECT c.id, c."customerName", c."customerEmail", c."createdAt",
       GREATEST(c."updatedAt", p."updatedAt") AS last_activity, p."profileJson" AS profile,
       (SELECT count(*) FROM "FragranceRecommendation" r WHERE r."conversationId" = c.id) AS recommendation_count,
       (SELECT count(*) FROM "FragranceRecommendation" r WHERE r."conversationId" = c.id AND r."shopifyProductId" IS NOT NULL) AS build_count
FROM "Conversation" c
JOIN "CustomerProfileState" p ON p."conversationId" = c.id
{where}
ORDER BY last_activity DESC, c.id
LIMIT :limit OFFSET :offset
"""
_COUNT_SQL = 'SELECT count(*) FROM "Conversation" c JOIN "CustomerProfileState" p ON p."conversationId" = c.id {where}'
_SEARCH_WHERE = """WHERE c."customerEmail" ILIKE :pattern ESCAPE '\\' OR c."customerName" ILIKE :pattern ESCAPE '\\'
   OR (p."profileJson"->>'email') ILIKE :pattern ESCAPE '\\' OR (p."profileJson"->>'name') ILIKE :pattern ESCAPE '\\'"""


def _like_pattern(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _profile(raw: Any) -> dict:
    if isinstance(raw, str):  # json (not jsonb) columns come back as text through text() queries
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    raw = raw if isinstance(raw, dict) else {}
    return {k: raw.get(k) for k in PROFILE_FIELDS}


@router.get("/admin/api/customers")
async def list_customers(
    q: str | None = Query(default=None, max_length=200), page: int = 1, page_size: int = Query(default=25, alias="pageSize"),
    merchant: MerchantSession = Depends(require_merchant), session: AsyncSession = Depends(get_session),
) -> dict:
    page, page_size = _page(page, page_size)
    term = (q or "").strip()
    where, params = ("", {}) if not term else (_SEARCH_WHERE, {"pattern": _like_pattern(term)})
    total = await session.scalar(text(_COUNT_SQL.format(where=where)), params)
    rows = (await session.execute(text(_LIST_SQL.format(where=where)), {**params, "limit": page_size, "offset": (page - 1) * page_size})).mappings().all()
    customers = []
    for row in rows:
        profile = _profile(row["profile"])
        customers.append({
            "conversationId": row["id"],
            "name": row["customerName"] or profile["name"],
            "email": row["customerEmail"] or profile["email"],
            "location": ", ".join(str(v) for v in (profile["city"], profile["stateRegion"], profile["country"]) if v) or None,
            "likes": profile["likes"] or [],
            "dislikes": profile["dislikes"] or [],
            "preferredStyle": profile["preferredStyle"] or profile["inferredStyle"],
            "occasion": profile["occasion"] or profile["giftRecipient"],
            "lastActivity": _iso(row["last_activity"]),
            "recommendationCount": row["recommendation_count"],
            "buildCount": row["build_count"],
        })
    return {"customers": customers, "total": total, "page": page, "pageSize": page_size}


async def _conversation_or_404(session: AsyncSession, conversation_id: str) -> Conversation:
    conversation = await session.scalar(select(Conversation).where(Conversation.id == conversation_id))
    if conversation is None:
        raise HTTPException(status_code=404, detail="customer not found", headers=_NO_STORE)
    return conversation


@router.get("/admin/api/customers/{conversation_id}")
async def customer_detail(conversation_id: str, merchant: MerchantSession = Depends(require_merchant), session: AsyncSession = Depends(get_session)) -> dict:
    conversation = await _conversation_or_404(session, conversation_id)
    state = await session.scalar(select(CustomerProfileState).where(CustomerProfileState.conversationId == conversation_id))
    profile = _profile(state.profileJson if state else {})
    counts = (await session.execute(
        select(func.count(FragranceRecommendation.id), func.count(FragranceRecommendation.shopifyProductId))
        .where(FragranceRecommendation.conversationId == conversation_id)
    )).one()
    message_count = await session.scalar(select(func.count(Message.id)).where(Message.conversationId == conversation_id, Message.role.in_(("user", "assistant"))))
    return {
        "conversationId": conversation.id,
        "name": conversation.customerName or profile["name"],
        "email": conversation.customerEmail or profile["email"],
        "createdAt": _iso(conversation.createdAt),
        "lastActivity": _iso(max(d for d in (conversation.updatedAt, state.updatedAt if state else None) if d)),
        "profile": profile,
        "recommendationCount": counts[0],
        "buildCount": counts[1],
        "messageCount": message_count,
    }


@router.get("/admin/api/customers/{conversation_id}/messages")
async def customer_messages(
    conversation_id: str, page: int = 1, page_size: int = Query(default=50, alias="pageSize"),
    merchant: MerchantSession = Depends(require_merchant), session: AsyncSession = Depends(get_session),
) -> dict:
    """Customer-visible turns only (user / assistant), newest page first, each page oldest-first."""
    await _conversation_or_404(session, conversation_id)
    page, page_size = _page(page, page_size)
    where = (Message.conversationId == conversation_id, Message.role.in_(("user", "assistant")))
    total = await session.scalar(select(func.count(Message.id)).where(*where))
    rows = (await session.scalars(
        select(Message).where(*where).order_by(Message.createdAt.desc(), Message.id.desc()).limit(page_size).offset((page - 1) * page_size)
    )).all()
    return {
        "messages": [{"role": m.role, "content": m.content, "createdAt": _iso(m.createdAt)} for m in reversed(rows)],
        "total": total, "page": page, "pageSize": page_size,
    }


def _snapshot(snapshot: RecommendationInventorySnapshot | None) -> dict | None:
    if snapshot is None:
        return None
    return {
        "checkedAt": _iso(snapshot.checkedAt),
        "requestStatus": snapshot.requestStatus,
        "inventoryValidated": snapshot.inventoryValidated,
        "buildable": snapshot.buildable,
        "oilTotalMl": snapshot.oilTotalMl,
        "alcoholMl": snapshot.alcoholMl,
        "maxBuildableBottles": snapshot.maxBuildableBottles,
        "limitingSku": snapshot.limitingSku,
        "components": [
            {
                "productTitle": c.productTitle, "odooSku": c.odooSku, "ratioPercent": c.ratioPercent, "requiredOilMl": c.requiredOilMl,
                "onHandQty": c.onHandQty, "mappingStatus": c.mappingStatus, "sufficient": c.sufficient,
                "maxBuildableBottlesForComponent": c.maxBuildableBottlesForComponent,
            }
            for c in snapshot.components
        ],
    }


def serialize_recommendation(r: FragranceRecommendation) -> dict:
    facing = r.customerFacingJson or {}
    score = r.scoreJson or {}
    return {
        "id": r.id,
        "conversationId": r.conversationId,
        "status": r.status,
        "buildStatus": r.buildStatus,
        "needsReview": r.buildStatus in ATTENTION_STATUSES,
        "combinationType": r.combinationType,
        "createdAt": _iso(r.createdAt),
        "confirmedAt": _iso(r.confirmedAt),
        "name": r.draftName or facing.get("customerFacingName"),
        "customerFacing": facing,
        # Internal (merchant only): real components, ratios, scoring, evidence.
        "products": r.productsJson,
        "ratios": r.ratiosJson,
        "draftRatios": r.draftRatiosJson,
        "draftExcludedNotes": r.draftExcludedNotes,
        "confidence": score.get("confidence"),
        "confidenceBreakdown": score.get("confidenceBreakdown"),
        "riskPenalty": score.get("riskPenalty"),
        "riskBreakdown": score.get("riskBreakdown"),
        "exactNotes": {
            "requested": score.get("requestedExactNotes"), "matched": score.get("matchedExactNotes"),
            "missing": score.get("missingExactNotes"), "coverageScore": score.get("exactNoteCoverageScore"),
        },
        "score": score,
        "evidence": r.evidenceJson,
        "evidenceScope": r.evidenceScope,
        "shopifyProductId": r.shopifyProductId,
        "shopifyVariantId": r.shopifyVariantId,
        "shopifyAdminUrl": _admin_product_url(r.shopifyProductId),
        "inventory": _snapshot(r.inventorySnapshot),
    }


def _with_snapshot(statement):
    return statement.options(selectinload(FragranceRecommendation.inventorySnapshot).selectinload(RecommendationInventorySnapshot.components))


@router.get("/admin/api/customers/{conversation_id}/recommendations")
async def customer_recommendations(
    conversation_id: str, page: int = 1, page_size: int = Query(default=20, alias="pageSize"),
    merchant: MerchantSession = Depends(require_merchant), session: AsyncSession = Depends(get_session),
) -> dict:
    """Every generated recommendation (pending, confirmed, expired) with its STORED inventory
    snapshot. Opening this never triggers an Odoo lookup."""
    await _conversation_or_404(session, conversation_id)
    page, page_size = _page(page, page_size)
    where = FragranceRecommendation.conversationId == conversation_id
    total = await session.scalar(select(func.count(FragranceRecommendation.id)).where(where))
    rows = (await session.scalars(
        _with_snapshot(select(FragranceRecommendation).where(where).order_by(FragranceRecommendation.createdAt.desc(), FragranceRecommendation.id).limit(page_size).offset((page - 1) * page_size))
    )).all()
    return {"recommendations": [serialize_recommendation(r) for r in rows], "total": total, "page": page, "pageSize": page_size}


# ---------------------------------------------------------------------------
# Activity / operations
# ---------------------------------------------------------------------------

@router.get("/admin/api/activity")
async def activity(
    page: int = 1, page_size: int = Query(default=25, alias="pageSize"), attention_only: bool = Query(default=False, alias="attentionOnly"),
    merchant: MerchantSession = Depends(require_merchant), session: AsyncSession = Depends(get_session),
) -> dict:
    """Recent recommendation and build outcomes from stored state (no log database). Builds in
    `creating` / `pending_review` are listed for manual review; nothing here retries them."""
    page, page_size = _page(page, page_size)
    conditions = []
    if attention_only:
        conditions.append(FragranceRecommendation.buildStatus.in_(ATTENTION_STATUSES))
    total = await session.scalar(select(func.count(FragranceRecommendation.id)).where(*conditions))
    rows = (await session.scalars(
        _with_snapshot(select(FragranceRecommendation).where(*conditions).order_by(FragranceRecommendation.createdAt.desc(), FragranceRecommendation.id).limit(page_size).offset((page - 1) * page_size))
    )).all()
    attention = await session.scalar(select(func.count(FragranceRecommendation.id)).where(FragranceRecommendation.buildStatus.in_(ATTENTION_STATUSES)))
    status_counts = dict((await session.execute(select(FragranceRecommendation.status, func.count()).group_by(FragranceRecommendation.status))).all())
    build_counts = dict((await session.execute(select(FragranceRecommendation.buildStatus, func.count()).group_by(FragranceRecommendation.buildStatus))).all())
    inventory_counts = dict((await session.execute(
        select(RecommendationInventorySnapshot.requestStatus, func.count()).group_by(RecommendationInventorySnapshot.requestStatus)
    )).all())
    items = []
    for r in rows:
        snap = r.inventorySnapshot
        items.append({
            "id": r.id, "conversationId": r.conversationId, "name": r.draftName or (r.customerFacingJson or {}).get("customerFacingName"),
            "status": r.status, "buildStatus": r.buildStatus, "needsReview": r.buildStatus in ATTENTION_STATUSES,
            "createdAt": _iso(r.createdAt), "confirmedAt": _iso(r.confirmedAt), "shopifyAdminUrl": _admin_product_url(r.shopifyProductId),
            "inventory": None if snap is None else {"requestStatus": snap.requestStatus, "inventoryValidated": snap.inventoryValidated, "buildable": snap.buildable, "checkedAt": _iso(snap.checkedAt)},
        })
    return {
        "items": items, "total": total, "page": page, "pageSize": page_size,
        "summary": {"needsReview": attention, "byStatus": status_counts, "byBuildStatus": build_counts, "inventoryLookups": inventory_counts},
    }


@router.get("/admin/api/readiness")
async def admin_readiness(merchant: MerchantSession = Depends(require_merchant), session: AsyncSession = Depends(get_session)) -> dict:
    from app.api.health import readiness_report

    return await readiness_report(session)
