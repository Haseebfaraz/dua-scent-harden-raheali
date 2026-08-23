"""Port of app/services/recommendationInventorySnapshot.server.js -- persists the Odoo
manufacturing-feasibility check as immutable historical evidence, one row per recommendation that
was ACTUALLY walked by the auto-select gate. Never updated after creation.

Pure persistence -- every value is computed exactly once by the caller
(odoo_inventory.evaluate_candidate_inventory) and just written here verbatim, so the logs and this
DB row can never drift apart. Component dicts are read via explicit key access only (never spread)
so an unexpected extra field on a buggy caller's component (e.g. a raw Odoo request payload
carrying an Authorization header) can never leak into the DB.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.ids import new_id
from app.db.models import RecommendationInventoryComponent, RecommendationInventorySnapshot
from app.db.time import utcnow


def _parse_checked_at(checked_at: str | datetime) -> datetime:
    if isinstance(checked_at, datetime):
        return checked_at
    return datetime.fromisoformat(checked_at.replace("Z", "+00:00")).replace(tzinfo=None)


async def save_inventory_snapshot(
    session: AsyncSession, *, recommendation_id: str, inventory_validated: bool, buildable: bool,
    checked_at: str | datetime, oil_total_ml: float, alcohol_ml: float, request_status: str,
    max_buildable_bottles: int | None, limiting_sku: str | None, components: list[dict[str, Any]],
) -> RecommendationInventorySnapshot:
    snapshot = RecommendationInventorySnapshot(
        id=new_id(),
        recommendationId=recommendation_id,
        inventoryValidated=inventory_validated,
        buildable=buildable,
        checkedAt=_parse_checked_at(checked_at),
        oilTotalMl=oil_total_ml,
        alcoholMl=alcohol_ml,
        maxBuildableBottles=max_buildable_bottles,
        limitingSku=limiting_sku,
        requestStatus=request_status,
        createdAt=utcnow(),
        components=[
            RecommendationInventoryComponent(
                id=new_id(),
                fragranceProductId=c.get("fragranceProductId"),
                productTitle=c["productTitle"],
                odooSku=c.get("odooSku"),
                ratioPercent=c["ratioPercent"],
                requiredOilMl=c["requiredOilMl"],
                onHandQty=c.get("onHandQty"),
                mappingStatus=c["mappingStatus"],
                sufficient=c.get("sufficient"),
                maxBuildableBottlesForComponent=c.get("maxBuildableBottlesForComponent"),
            )
            for c in components
        ],
    )
    session.add(snapshot)
    await session.commit()
    await session.refresh(snapshot, attribute_names=["components"])
    return snapshot


async def get_inventory_snapshot(session: AsyncSession, recommendation_id: str) -> RecommendationInventorySnapshot | None:
    return await session.scalar(
        select(RecommendationInventorySnapshot)
        .where(RecommendationInventorySnapshot.recommendationId == recommendation_id)
        .options(selectinload(RecommendationInventorySnapshot.components))
    )
