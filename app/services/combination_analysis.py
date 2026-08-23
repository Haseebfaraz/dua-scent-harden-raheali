"""Port of app/services/combinationAnalysis.server.js -- canonical existing-combination lookups.
All matching goes through create_combination_key/normalize_product_name so order and casing never
matter; every note comparison uses only FragranceProduct.notesJson -- never inferred notes.

Read-only. Does NOT contain combination generation/scoring -- see recommendation_engine.py for that.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ExistingCombination, FragranceProduct
from app.fragrance.combination_key import create_combination_key
from app.fragrance.normalization import normalize_product_name

_COMBO_COLUMNS = (
    ExistingCombination.title, ExistingCombination.type, ExistingCombination.componentProductsJson,
    ExistingCombination.tagLine, ExistingCombination.normalizedTitle,
)


async def _load_all_combinations(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(select(*_COMBO_COLUMNS))).all()
    return [
        {"title": t, "type": ty, "componentProductsJson": cpj, "tagLine": tl, "normalizedTitle": nt}
        for t, ty, cpj, tl, nt in rows
    ]


async def find_existing_combinations_for_product(session: AsyncSession, product_title: str) -> dict[str, Any]:
    normalized_title = normalize_product_name(product_title)
    combos = await _load_all_combinations(session)

    as_finished_combination = next((c for c in combos if c["normalizedTitle"] == normalized_title), None)
    as_component_in = [
        c for c in combos
        if c["normalizedTitle"] != normalized_title
        and isinstance(c["componentProductsJson"], list)
        and any(normalize_product_name(name) == normalized_title for name in c["componentProductsJson"])
    ]

    return {
        "productTitle": product_title,
        "asFinishedCombination": (
            {
                "title": as_finished_combination["title"], "type": as_finished_combination["type"],
                "tagLine": as_finished_combination["tagLine"], "componentProducts": as_finished_combination["componentProductsJson"],
            } if as_finished_combination else None
        ),
        "asComponentIn": [
            {"title": c["title"], "type": c["type"], "tagLine": c["tagLine"], "componentProducts": c["componentProductsJson"]}
            for c in as_component_in
        ],
    }


async def check_exact_combination_exists(session: AsyncSession, product_titles: list[str]) -> dict[str, Any]:
    component_key = create_combination_key(product_titles)
    existing = await session.scalar(select(ExistingCombination).where(ExistingCombination.componentKey == component_key))
    return {
        "componentKey": component_key,
        "exists": bool(existing),
        "existingCombination": (
            {"title": existing.title, "type": existing.type, "tagLine": existing.tagLine, "componentProducts": existing.componentProductsJson}
            if existing else None
        ),
    }


async def find_combinations_using_similar_notes(session: AsyncSession, product_title: str, limit: int = 5) -> dict[str, Any]:
    normalized_title = normalize_product_name(product_title)
    reference_product = await session.scalar(select(FragranceProduct).where(FragranceProduct.normalizedTitle == normalized_title))
    if not reference_product:
        return {"status": "NOT_FOUND", "message": "Notes data not found"}

    reference_notes = {str(n).lower() for n in (reference_product.notesJson if isinstance(reference_product.notesJson, list) else [])}
    if not reference_notes:
        return {"productTitle": product_title, "matches": []}

    combos = await _load_all_combinations(session)
    all_products = (await session.execute(select(FragranceProduct.normalizedTitle, FragranceProduct.notesJson))).all()
    notes_by_normalized_title = {nt: (nj if isinstance(nj, list) else []) for nt, nj in all_products}

    scored = []
    for c in combos:
        if c["normalizedTitle"] == normalized_title:
            continue
        component_notes = set()
        for name in (c["componentProductsJson"] if isinstance(c["componentProductsJson"], list) else []):
            for n in notes_by_normalized_title.get(normalize_product_name(name), []):
                component_notes.add(str(n).lower())
        overlapping_notes = [n for n in component_notes if n in reference_notes]
        if overlapping_notes:
            scored.append({"combo": c, "overlappingNotes": overlapping_notes})
    scored.sort(key=lambda r: -len(r["overlappingNotes"]))
    scored = scored[:limit]

    return {
        "productTitle": product_title,
        "matches": [
            {
                "title": r["combo"]["title"], "type": r["combo"]["type"], "tagLine": r["combo"]["tagLine"],
                "overlappingNotes": r["overlappingNotes"], "overlapCount": len(r["overlappingNotes"]),
            }
            for r in scored
        ],
    }
