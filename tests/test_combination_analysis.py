import pytest
from sqlalchemy import select

from app.db.models import ExistingCombination, FragranceProduct
from app.services.combination_analysis import (
    check_exact_combination_exists,
    find_combinations_using_similar_notes,
    find_existing_combinations_for_product,
)


# Phase 7: these tests need a product / hybrid / order-history row to exist, not the real catalog.
pytestmark = pytest.mark.usefixtures("synthetic_catalog")

@pytest.mark.asyncio
async def test_confirms_real_hybrid_regardless_of_order(db_session):
    real = await db_session.scalar(select(ExistingCombination).where(ExistingCombination.type == "HYBRID").limit(1))
    a, b = real.componentProductsJson[0], real.componentProductsJson[1]

    forward = await check_exact_combination_exists(db_session, [a, b])
    reverse = await check_exact_combination_exists(db_session, [b, a])

    assert forward["exists"] is True
    assert reverse["exists"] is True
    assert forward["componentKey"] == reverse["componentKey"]
    assert forward["existingCombination"]["title"] == real.title


@pytest.mark.asyncio
async def test_reports_new_combination_as_not_existing(db_session):
    products = (await db_session.execute(select(FragranceProduct.title).limit(2))).all()
    p1, p2 = products[0][0], products[1][0]
    result = await check_exact_combination_exists(db_session, [p1, f"{p2} (pytest fake suffix)"])
    assert result["exists"] is False
    assert result["existingCombination"] is None


@pytest.mark.asyncio
async def test_finds_combination_as_finished_and_via_components(db_session):
    real = await db_session.scalar(select(ExistingCombination).where(ExistingCombination.type == "HYBRID").limit(1))
    component_title = real.componentProductsJson[0]

    as_finished = await find_existing_combinations_for_product(db_session, real.title)
    assert as_finished["asFinishedCombination"] is not None
    assert as_finished["asFinishedCombination"]["title"] == real.title

    as_component = await find_existing_combinations_for_product(db_session, component_title)
    assert any(c["title"] == real.title for c in as_component["asComponentIn"])


@pytest.mark.asyncio
async def test_similar_notes_not_found_for_unknown_product(db_session):
    result = await find_combinations_using_similar_notes(db_session, "Totally Fake Product Name That Does Not Exist")
    assert result == {"status": "NOT_FOUND", "message": "Notes data not found"}


@pytest.mark.asyncio
async def test_similar_notes_ranked_by_overlap_count(db_session):
    result = await find_combinations_using_similar_notes(db_session, "The Opera", 5)
    assert isinstance(result["matches"], list)
    for i in range(1, len(result["matches"])):
        assert result["matches"][i - 1]["overlapCount"] >= result["matches"][i]["overlapCount"]
