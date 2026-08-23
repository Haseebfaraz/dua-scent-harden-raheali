"""Port of app/services/productCatalog.server.js -- backs the
get_product_notes_and_combination_status tool. Only ever returns notes already stored on
FragranceProduct -- never infers notes from a product's title or name. Point lookups only; does
NOT touch the 936k-row OrderHistory table.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ExistingCombination, FragranceProduct
from app.fragrance.normalization import normalize_product_name

# notesJson preserves the source spreadsheet's note order -- the first few entries are treated as
# "main" notes and the rest as "supporting," matching the same prominence convention
# app/fragrance/scoring.py's PROMINENT_NOTE_WINDOW already uses for dislike-conflict severity.
_MAIN_NOTE_COUNT = 3


async def _combinations_involving(session: AsyncSession, normalized_title: str) -> tuple[Any, list[Any]]:
    combos = (await session.scalars(select(ExistingCombination))).all()
    as_finished_combination = next((c for c in combos if c.normalizedTitle == normalized_title), None)
    as_component_in = [
        c for c in combos
        if c.normalizedTitle != normalized_title
        and isinstance(c.componentProductsJson, list)
        and any(normalize_product_name(name) == normalized_title for name in c.componentProductsJson)
    ]
    return as_finished_combination, as_component_in


async def get_product_notes_and_combination_status(session: AsyncSession, product_title: str) -> dict[str, Any]:
    normalized_title = normalize_product_name(product_title)
    product = await session.scalar(
        select(FragranceProduct).where(FragranceProduct.normalizedTitle == normalized_title)
    )
    if not product:
        return {"status": "NOT_FOUND", "message": "Notes data not found"}

    notes = product.notesJson if isinstance(product.notesJson, list) else []
    as_finished_combination, as_component_in = await _combinations_involving(session, normalized_title)

    matching_existing_combinations = []
    if as_finished_combination:
        matching_existing_combinations.append({
            "title": as_finished_combination.title,
            "type": as_finished_combination.type,
            "tagLine": as_finished_combination.tagLine,
            "role": "isThisProduct",
        })
    matching_existing_combinations.extend(
        {"title": c.title, "type": c.type, "tagLine": c.tagLine, "role": "component"} for c in as_component_in
    )

    return {
        "status": "FOUND",
        "title": product.title,
        "handle": product.handle,
        "mainNotes": notes[:_MAIN_NOTE_COUNT],
        "supportingNotes": notes[_MAIN_NOTE_COUNT:],
        "fragranceFamily": product.fragranceFamily,
        "collection": product.collection,
        "isSingleInspiration": product.isSingleInspiration,
        "tagLine": product.tagLine,
        "inspirationName": product.inspirationName,
        "inspirationBrand": product.inspirationBrand,
        "isHybrid": bool(as_finished_combination) and as_finished_combination.type == "HYBRID",
        "isTribrid": bool(as_finished_combination) and as_finished_combination.type == "TRIBRID",
        "isQuadbrid": bool(as_finished_combination) and as_finished_combination.type == "QUADBRID",
        "appearsAsComponentIn": [{"title": c.title, "type": c.type, "tagLine": c.tagLine} for c in as_component_in],
        "matchingExistingCombinations": matching_existing_combinations,
        "missingDataFlags": {
            "notes": len(notes) == 0,
            "fragranceFamily": not product.fragranceFamily,
            "pricePer5ml": product.pricePer5ml is None,
        },
    }
