"""Port of app/services/orderHistoryAnalysis.server.js -- deterministic product-candidate scoring
backing the analyze_customer_product_candidates tool. Every number here comes from real DB
aggregation -- never a full-table scan, and never a score invented by the language model.

Country/state/season/classification tiers read from ProductRegionSummary (precomputed) rather
than live-aggregating OrderHistory (936,819 rows) -- see that model's own comment. City tier stays
live: a single real city's row count is always small.

ponytail: the JS original fires its per-tier/per-dimension queries concurrently via Promise.all.
A single SQLAlchemy AsyncSession can't safely run overlapping queries (one asyncpg connection,
one statement at a time), so these run sequentially here. All are cheap indexed point-reads, and
the "completes within 15s" parity test still passes comfortably sequential -- revisit with a
connection-per-query gather if this ever becomes the bottleneck.
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FragranceProduct, OrderHistory, ProductRegionSummary
from app.fragrance.compatibility import (
    count_preferred_direction_matches,
    interpret_customer_preferences,
    interpret_lifestyle_context,
    literal_note_match_count,
    literal_note_terms_from_likes,
    passes_intensity_filter,
    split_dislikes_by_exactness,
    text_to_preference_families,
)
from app.fragrance.normalization import SEASON_ALIASES
from app.fragrance.scoring import SCORE_WEIGHTS, classify_dislike_conflict, compute_evidence_level, like_match_strength, matched_likes

# Matches the MIN_SAMPLE_SIZE convention already established for regional signals -- a regional
# signal only counts once it's backed by a real sample.
MIN_SAMPLE_SIZE = 20
# Bounded shortlist per region tier -- the candidate pool is a union of each tier's top products,
# never every distinct product in the table, so downstream per-candidate queries stay small.
CANDIDATE_SHORTLIST_PER_TIER = 20
MAX_CANDIDATES_RETURNED = 15
LIKE_MATCH_SHORTLIST = 20
# "Popular among similar customers" (breadth) is distinguished from "repeat purchase by a similar
# customer" (loyalty, one customer buying it more than once) by requiring several distinct
# customers in the region, not just one.
POPULARITY_THRESHOLD = 5
LITERAL_MATCH_BOOST = 0.5


# ---- City tier (live -- always a small, fast partition of the table) ----


async def _top_products_by_city_live(session: AsyncSession, city: str | None, limit: int) -> list[str]:
    if not city:
        return []
    rows = await session.execute(
        select(OrderHistory.normalizedProductName, func.count().label("cnt"))
        .where(OrderHistory.city == city, OrderHistory.normalizedProductName.is_not(None))
        .group_by(OrderHistory.normalizedProductName)
        .order_by(func.count().desc())
        .limit(limit)
    )
    return [r[0] for r in rows.all()]


async def _city_counts_by_product(session: AsyncSession, city: str | None, candidate_names: list[str]) -> dict[str, int]:
    if not city or not candidate_names:
        return {}
    rows = await session.execute(
        select(OrderHistory.normalizedProductName, func.count().label("cnt"))
        .where(OrderHistory.city == city, OrderHistory.normalizedProductName.in_(candidate_names))
        .group_by(OrderHistory.normalizedProductName)
    )
    return {r[0]: r[1] for r in rows.all()}


# ---- Preference-match tier (real catalog, ranked by how much it reflects stated likes) ----


async def _top_products_by_like_match(
    session: AsyncSession, like_families: list[str], literal_terms: list[str]
) -> list[str]:
    if not like_families:
        return []
    rows = (await session.execute(select(FragranceProduct.normalizedTitle, FragranceProduct.notesJson))).all()
    scored = []
    for normalized_title, notes_json in rows:
        notes = notes_json if isinstance(notes_json, list) else []
        strength = max([0.0] + [like_match_strength(notes, family) for family in like_families])
        if strength <= 0:
            continue
        rank = strength + LITERAL_MATCH_BOOST * literal_note_match_count(notes, literal_terms)
        scored.append((normalized_title, rank))
    scored.sort(key=lambda p: -p[1])
    return [name for name, _ in scored[:LIKE_MATCH_SHORTLIST]]


# ---- Country/state/season tiers (precomputed -- indexed point-reads, not live aggregation) ----


def _scope_values(scope_value: str | list[str] | None) -> list[str] | None:
    if scope_value is None:
        return None
    return scope_value if isinstance(scope_value, list) else [scope_value]


async def _top_products_from_summary(
    session: AsyncSession, scope: str, scope_value: str | list[str] | None, limit: int
) -> list[str]:
    values = _scope_values(scope_value)
    if not values:
        return []
    rows = await session.execute(
        select(ProductRegionSummary.normalizedProductName)
        .where(ProductRegionSummary.scope == scope, ProductRegionSummary.scopeValue.in_(values))
        .order_by(ProductRegionSummary.orderCount.desc())
        .limit(limit)
    )
    return [r[0] for r in rows.all()]


async def _summary_by_product(
    session: AsyncSession, scope: str | None, scope_value: str | list[str] | None, candidate_names: list[str]
) -> dict[str, dict[str, int]]:
    values = _scope_values(scope_value)
    if not scope or not values or not candidate_names:
        return {}
    rows = await session.execute(
        select(
            ProductRegionSummary.normalizedProductName,
            ProductRegionSummary.orderCount,
            ProductRegionSummary.distinctCustomerCount,
            ProductRegionSummary.repeatCustomerCount,
        ).where(
            ProductRegionSummary.scope == scope,
            ProductRegionSummary.scopeValue.in_(values),
            ProductRegionSummary.normalizedProductName.in_(candidate_names),
        )
    )
    # Multiple alias rows (e.g. "Summer" + "Summer Months") can both match one product -- combine
    # into a single real total rather than keeping only whichever alias happened to load last.
    combined: dict[str, dict[str, int]] = {}
    for name, order_count, distinct_count, repeat_count in rows.all():
        existing = combined.get(name)
        if not existing:
            combined[name] = {
                "orderCount": order_count,
                "distinctCustomerCount": distinct_count,
                "repeatCustomerCount": repeat_count,
            }
        else:
            existing["orderCount"] += order_count
            existing["distinctCustomerCount"] += distinct_count
            existing["repeatCustomerCount"] += repeat_count
    return combined


async def _resolve_cohort_evidence(
    session: AsyncSession,
    city: str | None,
    state_region: str | None,
    country: str | None,
    candidate_names: list[str],
) -> dict[str, dict[str, int]]:
    if city:
        city_order_count = await session.scalar(select(func.count()).where(OrderHistory.city == city))
        if city_order_count and city_order_count >= MIN_SAMPLE_SIZE:
            rows = await session.execute(
                select(OrderHistory.normalizedProductName, OrderHistory.customerKeyHash, func.count().label("cnt"))
                .where(
                    OrderHistory.city == city,
                    OrderHistory.normalizedProductName.in_(candidate_names),
                    OrderHistory.customerKeyHash.is_not(None),
                )
                .group_by(OrderHistory.normalizedProductName, OrderHistory.customerKeyHash)
            )
            distinct: dict[str, int] = {}
            repeat: dict[str, int] = {}
            for name, _customer_key_hash, count in rows.all():
                distinct[name] = distinct.get(name, 0) + 1
                if count > 1:
                    repeat[name] = repeat.get(name, 0) + 1
            return {"distinct": distinct, "repeat": repeat}

    tier_scope = "state" if state_region else ("country" if country else None)
    tier_value = state_region or country or None
    summary = await _summary_by_product(session, tier_scope, tier_value, candidate_names)
    distinct = {name: row["distinctCustomerCount"] for name, row in summary.items()}
    repeat = {name: row["repeatCustomerCount"] for name, row in summary.items()}
    return {"distinct": distinct, "repeat": repeat}


async def analyze_customer_product_candidates(session: AsyncSession, profile: dict[str, Any]) -> list[dict[str, Any]]:
    """profile: CustomerFragranceProfile-shaped dict with city/stateRegion/country/season/likes/
    dislikes. All location fields are expected to already be confirmed/normalized real values --
    this function does no location fuzzing itself.
    """
    profile = profile or {}
    city = profile.get("city")
    state_region = profile.get("stateRegion")
    country = profile.get("country")
    season = profile.get("season")
    likes = profile.get("likes") or []
    dislikes = profile.get("dislikes") or []

    season_values = SEASON_ALIASES.get(season) if season else None
    like_families_for_candidates = text_to_preference_families(likes)
    literal_like_terms = literal_note_terms_from_likes(likes)

    city_top = await _top_products_by_city_live(session, city, CANDIDATE_SHORTLIST_PER_TIER)
    state_top = await _top_products_from_summary(session, "state", state_region, CANDIDATE_SHORTLIST_PER_TIER)
    country_top = await _top_products_from_summary(session, "country", country, CANDIDATE_SHORTLIST_PER_TIER)
    season_top = await _top_products_from_summary(session, "season", season_values, CANDIDATE_SHORTLIST_PER_TIER)
    like_match_top = await _top_products_by_like_match(session, like_families_for_candidates, literal_like_terms)

    candidate_names = list(dict.fromkeys([*city_top, *state_top, *country_top, *season_top, *like_match_top]))
    if not candidate_names:
        return []

    city_counts = await _city_counts_by_product(session, city, candidate_names)
    state_summary = await _summary_by_product(session, "state", state_region, candidate_names)
    country_summary = await _summary_by_product(session, "country", country, candidate_names)
    season_summary = await _summary_by_product(session, "season", season_values, candidate_names)
    classification_rows = (
        await session.execute(
            select(
                ProductRegionSummary.normalizedProductName,
                ProductRegionSummary.scopeValue,
                ProductRegionSummary.orderCount,
            ).where(
                ProductRegionSummary.scope == "classification_global",
                ProductRegionSummary.normalizedProductName.in_(candidate_names),
            )
        )
    ).all()
    cohort = await _resolve_cohort_evidence(session, city, state_region, country, candidate_names)
    products = (
        await session.execute(
            select(
                FragranceProduct.title,
                FragranceProduct.normalizedTitle,
                FragranceProduct.notesJson,
                FragranceProduct.collection,
            ).where(FragranceProduct.normalizedTitle.in_(candidate_names))
        )
    ).all()

    top_classification_by_product: dict[str, dict[str, Any]] = {}
    for name, scope_value, order_count in classification_rows:
        current = top_classification_by_product.get(name)
        if not current or order_count > current["orderCount"]:
            top_classification_by_product[name] = {"classification": scope_value, "orderCount": order_count}

    product_by_normalized_title = {p.normalizedTitle: p for p in products}

    like_families = like_families_for_candidates
    split = split_dislikes_by_exactness(dislikes)
    exact_note_dislikes = split["exactNoteDislikes"]
    dislike_families = split["explicitFamilyDislikes"]
    preference_intent = interpret_customer_preferences(profile)
    lifestyle_context = interpret_lifestyle_context(profile)

    candidates = []
    for normalized_product_name in candidate_names:
        product = product_by_normalized_title.get(normalized_product_name)
        if not product:
            continue

        notes = product.notesJson if isinstance(product.notesJson, list) else []
        same_city_orders = city_counts.get(normalized_product_name, 0)
        same_state_orders = state_summary.get(normalized_product_name, {}).get("orderCount", 0)
        same_country_orders = country_summary.get(normalized_product_name, {}).get("orderCount", 0)
        same_season_orders = season_summary.get(normalized_product_name, {}).get("orderCount", 0)
        distinct_similar_customers = cohort["distinct"].get(normalized_product_name, 0)
        repeat_purchase_customers = cohort["repeat"].get(normalized_product_name, 0)

        preference_matches = matched_likes(notes, like_families)
        dislike_conflict = classify_dislike_conflict(notes, dislike_families)

        if literal_note_match_count(notes, exact_note_dislikes) > 0:
            continue
        if dislike_conflict["severity"] == "high":
            continue
        if not passes_intensity_filter(notes, preference_intent):
            continue

        relevance_score = 0.0
        if same_city_orders > 0:
            relevance_score += SCORE_WEIGHTS["sameCity"]
        if same_country_orders > 0:
            relevance_score += SCORE_WEIGHTS["sameCountry"]
        if same_state_orders > 0:
            relevance_score += SCORE_WEIGHTS["sameStateRegionOrClimate"]
        if same_season_orders > 0:
            relevance_score += SCORE_WEIGHTS["sameSeason"]
        relevance_score += len(preference_matches) * SCORE_WEIGHTS["matchesLike"]
        relevance_score += literal_note_match_count(notes, literal_like_terms) * SCORE_WEIGHTS["matchesLike"] * LITERAL_MATCH_BOOST
        relevance_score += len(dislike_conflict["matchedFamilies"]) * SCORE_WEIGHTS["conflictsDislike"]
        if repeat_purchase_customers > 0:
            relevance_score += SCORE_WEIGHTS["repeatPurchaseBySimilarCustomer"]
        if distinct_similar_customers >= POPULARITY_THRESHOLD:
            relevance_score += SCORE_WEIGHTS["popularAmongSimilarCustomers"]

        for direction, weight in lifestyle_context["preferredDirections"].items():
            if count_preferred_direction_matches(notes, [direction]) > 0:
                relevance_score += 2 * weight

        candidates.append({
            "productName": product.title,
            "normalizedProductName": normalized_product_name,
            "collection": product.collection,
            "relevanceScore": relevance_score,
            "sameCityOrders": same_city_orders,
            "sameStateOrders": same_state_orders,
            "sameCountryOrders": same_country_orders,
            "sameSeasonOrders": same_season_orders,
            "distinctSimilarCustomers": distinct_similar_customers,
            "repeatPurchaseCustomers": repeat_purchase_customers,
            "preferenceMatches": preference_matches,
            "dislikeConflicts": dislike_conflict["matchedFamilies"],
            "classification": (top_classification_by_product.get(normalized_product_name) or {}).get("classification"),
            "orderHistoryNotes": notes,
            "evidenceLevel": compute_evidence_level(distinct_similar_customers, same_season_orders),
        })

    def _volume(c: dict[str, Any]) -> int:
        return c["sameCityOrders"] + c["sameStateOrders"] + c["sameCountryOrders"] + c["sameSeasonOrders"]

    candidates.sort(key=lambda c: (-c["relevanceScore"], -_volume(c)))
    return candidates[:MAX_CANDIDATES_RETURNED]
