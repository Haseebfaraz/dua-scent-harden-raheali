"""One orchestration for every controlled commerce action on a build (Phase 5, F9).

Both browser entry points -- the App-Proxied preview POST (save_build / add_to_cart) and the direct
POST /api/save-build -- go through execute_build_commerce. Callers must ALREADY have validated the
request, resolved the trusted shop and authorized the build capability for this recommendation;
nothing here is authorization, and nothing here is reached by an unauthorized caller (so an id
alone can never trigger an inventory lookup).

What this adds around the Shopify write layer (which carries the inventory gate itself):

  * one commerce operation per recommendation at a time, across instances (PostgreSQL advisory
    lock, same mechanism as the chat turn lock). A second overlapping request gets a conflict, it
    is not queued. THE LOCK SERIALIZES THIS APPLICATION ONLY. It does not reserve stock in Odoo
    and does not stop any other channel consuming it.
  * the recommendation is re-read INSIDE the lock, so a request that raced a first-time creation
    sees the product that now exists instead of creating a second one.
  * first-time creation is tracked in FragranceRecommendation.buildStatus:
        draft -> creating -> saved
                        \\-> pending_review   (a write was sent and its outcome is unknown, or a
                                               product exists but a required later step failed)
    `creating` left behind by a crashed process is treated exactly like pending_review. Neither
    is ever retried automatically: a blind retry of a creation mutation can mint a duplicate
    product. Recovery is an operator action (docs/INVENTORY_COMMERCE_SECURITY.md section 8).
  * an ambiguous repricing needs no state: the next attempt re-reads the product's variants and
    reuses the one that was created.

There is no idempotency key. Nothing a caller sends selects or reuses a previous result.

Phase 5A:
  * the customer's DRAFT (name, ratios) is shared build state, so it is now written INSIDE the
    lock, by this module, after the pending-review check. A request that is refused with a
    conflict has changed nothing. (Before, preview.py saved the draft first and then hit the
    lock, so a refused request could still overwrite the draft of the operation in progress.)
  * each operation works on its own immutable snapshot of the authorized inputs: the ratios and
    name passed in are the ones validated, verified against inventory, priced, sent to Shopify
    and stored. The stored draft is never read back to choose them.
  * the advisory lock dies with its connection, so it is NOT what prevents a duplicate creation
    after a crash. The durable marker is: `creating` is committed BEFORE the creation request can
    be sent, and the Shopify product id is committed the moment Shopify returns it.
"""

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import engine
from app.services.recommendation_confirmation import get_recommendation, mark_recommendation_draft, mark_recommendation_saved
from app.shopify.builds import BuildProductNotSaved, BuildWriteAmbiguous, create_shopify_build_product, reprice_existing_build
from app.shopify.products import get_product_handle

logger = logging.getLogger(__name__)

_LOCK_CLASS = 2  # class 1 is the conversation turn lock
BUILD_STATUS_CREATING = "creating"
BUILD_STATUS_PENDING_REVIEW = "pending_review"


class BuildOperationInProgress(Exception):
    """Another commerce operation for this recommendation is running right now."""


class BuildPendingReview(Exception):
    """An earlier creation could not be confirmed; automatic retries are refused."""


@asynccontextmanager
async def build_commerce_lock(recommendation_id: str) -> AsyncIterator[None]:
    connection = await engine.connect()
    acquired = False
    try:
        acquired = bool(await connection.scalar(text("SELECT pg_try_advisory_lock(:cls, hashtext(:rid))"), {"cls": _LOCK_CLASS, "rid": recommendation_id}))
        if not acquired:
            raise BuildOperationInProgress()
        yield
    finally:
        try:
            if acquired:
                await connection.execute(text("SELECT pg_advisory_unlock(:cls, hashtext(:rid))"), {"cls": _LOCK_CLASS, "rid": recommendation_id})
        except Exception:  # noqa: BLE001 -- closing the connection releases the lock anyway
            pass
        finally:
            await connection.close()


async def _set_build_status(session: AsyncSession, recommendation: Any, status: str, *, product_id: str | None = None) -> None:
    recommendation.buildStatus = status
    if product_id:
        recommendation.shopifyProductId = product_id
    await session.commit()


async def execute_build_commerce(
    session: AsyncSession, shop: str, *, recommendation_id: str, ratios: dict[str, int], name: str | None,
    customer_name: str | None = None, customer_email: str | None = None, allow_create: bool = True, want_product_url: bool = True, record_saved: bool = True,
    save_draft: bool = False,
) -> dict[str, Any]:
    """Returns {"productId", "variantId", "productUrl", "price", "created"} only after the
    operation really succeeded. Raises InventoryNotVerified, BuildOperationInProgress,
    BuildPendingReview, BuildWriteAmbiguous, or the validation / Shopify errors of the write layer.
    """
    async with build_commerce_lock(recommendation_id):
        recommendation = await get_recommendation(session, recommendation_id)
        if recommendation is None:
            raise LookupError("recommendation")
        if sa_inspect(recommendation, raiseerr=False) is not None:
            await session.refresh(recommendation)  # state as of NOW, inside the lock

        if recommendation.buildStatus in (BUILD_STATUS_CREATING, BUILD_STATUS_PENDING_REVIEW):
            logger.warning("BUILD_COMMERCE_PENDING_REVIEW %s", json.dumps({"recommendationId": recommendation_id, "buildStatus": recommendation.buildStatus}))
            raise BuildPendingReview()

        # The operation's snapshot. Copied so nothing can alter it between verification and write.
        ratios = dict(ratios)
        if save_draft:
            await mark_recommendation_draft(session, recommendation_id, name=name, ratios=dict(ratios))

        if recommendation.shopifyProductId:
            reprice = await reprice_existing_build(session, shop, recommendation=recommendation, ratios=ratios, name=name)
            handle = await get_product_handle(session, shop, recommendation.shopifyProductId) if want_product_url else None
            if record_saved:  # the direct save-build endpoint never persisted the chosen variant; unchanged
                await mark_recommendation_saved(session, recommendation_id, shopify_product_id=recommendation.shopifyProductId, shopify_variant_id=reprice["variantId"])
            return {
                "productId": recommendation.shopifyProductId, "variantId": reprice["variantId"], "price": reprice["price"],
                "created": reprice["created"], "productUrl": f"https://{shop}/products/{handle}" if handle else None,
            }

        if not allow_create:
            raise BuildProductNotSaved("This fragrance hasn't been created yet — please save it from the preview first.")

        previous_status = recommendation.buildStatus
        # DURABLE before the creation request can be sent: a process that dies anywhere after this
        # line leaves `creating`, which every later attempt treats as "needs review".
        await _set_build_status(session, recommendation, BUILD_STATUS_CREATING)

        async def _record_remote_product(product_id: str) -> None:
            await _set_build_status(session, recommendation, BUILD_STATUS_CREATING, product_id=product_id)

        try:
            result = await create_shopify_build_product(
                session, shop, recommendation=recommendation,
                custom_name=name or (recommendation.customerFacingJson or {}).get("customerFacingName") or "Custom Blend",
                ratios=ratios, customer_name=customer_name, customer_email=customer_email, on_product_created=_record_remote_product,
            )
        except BuildWriteAmbiguous as err:
            await _set_build_status(session, recommendation, BUILD_STATUS_PENDING_REVIEW, product_id=err.product_id)
            logger.error("BUILD_COMMERCE_AMBIGUOUS %s", json.dumps({"recommendationId": recommendation_id, "productKnown": bool(err.product_id)}))
            raise
        except Exception:
            # (A cancellation is NOT caught here: the status stays `creating`, i.e. needs review.)
            # Refused before any write (inventory, validation, a definitive Shopify rejection):
            # nothing was created, so the build simply goes back to what it was.
            await session.rollback()
            await _set_build_status(session, recommendation, previous_status or "draft")
            raise
        await mark_recommendation_saved(session, recommendation_id, shopify_product_id=result["productId"], shopify_variant_id=result["variantId"])
        return {"productId": result["productId"], "variantId": result["variantId"], "price": result["price"], "created": True, "productUrl": result["productUrl"]}


async def save_recreate_draft(session: AsyncSession, *, recommendation_id: str, name: str | None, ratios: dict[str, int] | None) -> None:
    """The preview page's "recreate" intent only stores a draft, but the draft is shared build
    state, so it takes the same lock and respects the same markers as a commerce operation."""
    async with build_commerce_lock(recommendation_id):
        recommendation = await get_recommendation(session, recommendation_id)
        if recommendation is None:
            raise LookupError("recommendation")
        if sa_inspect(recommendation, raiseerr=False) is not None:
            await session.refresh(recommendation)
        if recommendation.buildStatus in (BUILD_STATUS_CREATING, BUILD_STATUS_PENDING_REVIEW):
            raise BuildPendingReview()
        await mark_recommendation_draft(session, recommendation_id, name=name, ratios=dict(ratios) if ratios else None)
