import time

import pytest
from sqlalchemy import select

from app.db.models import ProductRegionSummary
from app.services.order_history import analyze_customer_product_candidates

pytestmark = pytest.mark.usefixtures("synthetic_catalog")

ACCEPTANCE_PROFILE = {
    "city": "Los Angeles",
    "stateRegion": "California",
    "country": "United States",
    "season": "Summer",
    "likes": ["Fruity", "Sweet"],
    "dislikes": ["Spicy", "Strong"],
}


@pytest.mark.asyncio
async def test_returns_real_catalog_backed_candidates(db_session):
    candidates = await analyze_customer_product_candidates(db_session, ACCEPTANCE_PROFILE)
    assert len(candidates) > 0
    assert len(candidates) <= 15
    for c in candidates:
        assert isinstance(c["productName"], str)
        assert len(c["productName"]) > 0
        assert isinstance(c["orderHistoryNotes"], list)


@pytest.mark.reference_data  # asserts what the REAL catalog contains; see docs/PLATFORM_MODERNIZATION.md
@pytest.mark.asyncio
async def test_carries_real_per_dimension_evidence_counts(db_session):
    candidates = await analyze_customer_product_candidates(db_session, ACCEPTANCE_PROFILE)
    top = candidates[0]
    assert top["sameCityOrders"] >= 0
    assert top["sameCountryOrders"] >= 0
    assert top["sameStateOrders"] >= 0
    assert top["sameSeasonOrders"] >= 0
    assert top["sameCountryOrders"] > 0


@pytest.mark.asyncio
async def test_dislike_conflicts_always_a_list(db_session):
    candidates = await analyze_customer_product_candidates(db_session, ACCEPTANCE_PROFILE)
    assert all(isinstance(c["dislikeConflicts"], list) for c in candidates)


@pytest.mark.asyncio
async def test_ranks_by_relevance_score_tie_broken_by_volume(db_session):
    candidates = await analyze_customer_product_candidates(db_session, ACCEPTANCE_PROFILE)
    for i in range(1, len(candidates)):
        prev, cur = candidates[i - 1], candidates[i]
        assert prev["relevanceScore"] >= cur["relevanceScore"]
        if prev["relevanceScore"] == cur["relevanceScore"]:
            def volume(c):
                return c["sameCityOrders"] + c["sameStateOrders"] + c["sameCountryOrders"] + c["sameSeasonOrders"]
            assert volume(prev) >= volume(cur)


@pytest.mark.asyncio
async def test_falls_back_to_season_only_evidence_when_region_matches_nothing(db_session):
    candidates = await analyze_customer_product_candidates(db_session, {
        "city": "Nonexistent Fake City",
        "stateRegion": "Nowhere",
        "country": "Nonexistent Fake Country",
        "season": "Summer",
        "likes": ["Fruity"],
        "dislikes": [],
    })
    assert len(candidates) > 0
    for c in candidates:
        assert c["sameCityOrders"] == 0
        assert c["sameStateOrders"] == 0
        assert c["sameCountryOrders"] == 0


@pytest.mark.asyncio
async def test_empty_array_when_no_signal_at_all(db_session):
    candidates = await analyze_customer_product_candidates(db_session, {
        "city": "Nonexistent Fake City",
        "stateRegion": "Nowhere",
        "country": "Nonexistent Fake Country",
        "season": "NotARealSeason",
        "likes": [],
        "dislikes": [],
    })
    assert candidates == []


@pytest.mark.asyncio
async def test_finds_candidates_from_likes_alone_zero_region_signal(db_session):
    candidates = await analyze_customer_product_candidates(db_session, {
        "city": "Nonexistent Fake City",
        "stateRegion": "Nowhere",
        "country": "Nonexistent Fake Country",
        "season": "NotARealSeason",
        "likes": ["Fruity"],
        "dislikes": [],
    })
    assert len(candidates) > 0
    for c in candidates:
        assert c["sameCityOrders"] == 0
        assert c["sameStateOrders"] == 0
        assert c["sameCountryOrders"] == 0
        assert c["sameSeasonOrders"] == 0
        assert "fruity" in c["preferenceMatches"]


@pytest.mark.asyncio
async def test_finds_floral_candidates_from_likes_alone(db_session):
    candidates = await analyze_customer_product_candidates(db_session, {
        "city": "Nonexistent Fake City",
        "stateRegion": "Nowhere",
        "country": "Nonexistent Fake Country",
        "season": "NotARealSeason",
        "likes": ["Floral"],
        "dislikes": [],
    })
    assert len(candidates) > 0
    for c in candidates:
        assert c["sameCityOrders"] == 0
        assert c["sameStateOrders"] == 0
        assert c["sameCountryOrders"] == 0
        assert c["sameSeasonOrders"] == 0
        assert "floral" in c["preferenceMatches"]


@pytest.mark.asyncio
async def test_completes_within_old_live_aggregation_budget(db_session):
    start = time.monotonic()
    await analyze_customer_product_candidates(db_session, ACCEPTANCE_PROFILE)
    elapsed_ms = (time.monotonic() - start) * 1000
    assert elapsed_ms < 15000


@pytest.mark.asyncio
async def test_season_alias_sums_include_summer_and_summer_months(db_session):
    candidates = await analyze_customer_product_candidates(db_session, ACCEPTANCE_PROFILE)
    with_season_evidence = next((c for c in candidates if c["sameSeasonOrders"] > 0), None)
    assert with_season_evidence is not None

    summer_only = await db_session.scalar(
        select(ProductRegionSummary).where(
            ProductRegionSummary.normalizedProductName == with_season_evidence["normalizedProductName"],
            ProductRegionSummary.scope == "season",
            ProductRegionSummary.scopeValue == "Summer",
        )
    )
    summer_months_only = await db_session.scalar(
        select(ProductRegionSummary).where(
            ProductRegionSummary.normalizedProductName == with_season_evidence["normalizedProductName"],
            ProductRegionSummary.scope == "season",
            ProductRegionSummary.scopeValue == "Summer Months",
        )
    )
    expected_total = (summer_only.orderCount if summer_only else 0) + (summer_months_only.orderCount if summer_months_only else 0)
    assert with_season_evidence["sameSeasonOrders"] == expected_total
    if summer_months_only and summer_months_only.orderCount > 0:
        assert with_season_evidence["sameSeasonOrders"] > (summer_only.orderCount if summer_only else 0)
