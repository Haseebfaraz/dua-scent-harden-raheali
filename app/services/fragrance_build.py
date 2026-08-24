"""Port of the pure/DB-read parts of app/services/fragranceBuild.server.js -- note-position
bucketing, default ratio split, and per-position $/5ml pricing. The Shopify-Admin-GraphQL parts
of that same JS file (first-time product creation) live in app/shopify/builds.py instead, per the
rule that Shopify HTTP logic never mixes into recommendation/fragrance services. Shared by both
the Save Build flow (app/shopify/builds.py) and the preview page (app/api/preview.py), exactly as
the JS original is shared by fragrance-preview's loader and createShopifyBuildProduct.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FragranceProduct
from app.fragrance.compatibility import literal_note_terms_from_likes, text_to_preference_families
from app.fragrance.normalization import normalize_product_name
from app.fragrance.note_positions import assign_note_positions, classify_note

BOTTLE_ML = 34
FALLBACK_PRICE_PER_5ML = 20


def compute_note_position_buckets(internal_products: list[dict], customer_likes: list[str] | None = None) -> dict:
    all_notes = [n for p in (internal_products or []) for n in (p.get("notes") or [])]
    like_families = text_to_preference_families(customer_likes or [])
    literal_terms = literal_note_terms_from_likes(customer_likes or [])
    return assign_note_positions(all_notes, like_families, literal_terms)


def compute_default_ratios(buckets: dict[str, list]) -> dict[str, int]:
    counts = {p: len(buckets.get(p) or []) or 1 for p in ("top", "middle", "base")}
    total = sum(counts.values())
    raw = {p: (counts[p] / total) * 100 for p in counts}
    rounded = {p: round(raw[p]) for p in raw}
    diff = 100 - sum(rounded.values())
    if diff:
        largest = max(rounded, key=rounded.get)
        rounded[largest] += diff
    return rounded


async def compute_price_per_5ml_by_position(
    session: AsyncSession, internal_products: list[dict], ratios_by_product: list[dict]
) -> dict[str, float]:
    products = internal_products or []
    normalized_titles = [normalize_product_name(p.get("title")) for p in products]
    rows = (
        await session.execute(
            select(FragranceProduct.normalizedTitle, FragranceProduct.pricePer5ml).where(
                FragranceProduct.normalizedTitle.in_(normalized_titles)
            )
        )
    ).all()
    price_by_normalized_title: dict[str, Any] = {r[0]: r[1] for r in rows}
    ratio_by_normalized_title = {
        normalize_product_name(r.get("productTitle")): r.get("ratioPercent") for r in (ratios_by_product or [])
    }

    position_cost = {"top": 0.0, "middle": 0.0, "base": 0.0}
    position_ml = {"top": 0.0, "middle": 0.0, "base": 0.0}

    for product in products:
        normalized_title = normalize_product_name(product.get("title"))
        price = price_by_normalized_title.get(normalized_title)
        price_per_5ml = price if isinstance(price, (int, float)) else FALLBACK_PRICE_PER_5ML
        ratio_percent = ratio_by_normalized_title.get(normalized_title)
        if ratio_percent is None:
            ratio_percent = 100 / (len(products) or 1)
        product_ml = (ratio_percent / 100) * BOTTLE_ML
        product_cost = (product_ml / 5) * price_per_5ml

        note_counts = {"top": 0, "middle": 0, "base": 0}
        for note in product.get("notes") or []:
            note_counts[classify_note(note)] += 1
        total_notes = sum(note_counts.values())

        for position in ("top", "middle", "base"):
            share = (note_counts[position] / total_notes) if total_notes > 0 else 1 / 3
            position_ml[position] += product_ml * share
            position_cost[position] += product_cost * share

    return {
        position: (position_cost[position] / position_ml[position]) * 5 if position_ml[position] > 0 else FALLBACK_PRICE_PER_5ML
        for position in ("top", "middle", "base")
    }
