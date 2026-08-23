import time
import uuid

import pytest
from sqlalchemy import delete, select

from app.db.ids import new_id
from app.db.models import ExistingCombination, FragranceProduct
from app.db.time import utcnow
from app.fragrance.normalization import normalize_product_name
from app.services.product_catalog import get_product_notes_and_combination_status


@pytest.mark.asyncio
async def test_not_found_exact_shape(db_session):
    result = await get_product_notes_and_combination_status(db_session, "Totally Fake Product Name That Does Not Exist")
    assert result == {"status": "NOT_FOUND", "message": "Notes data not found"}


@pytest.mark.asyncio
async def test_returns_real_stored_notes_never_inferred(db_session):
    real = await db_session.scalar(
        select(FragranceProduct)
        .where(FragranceProduct.notesJson.is_not(None))
        .where(~FragranceProduct.title.contains("pytest"))
        .limit(1)
    )
    assert real is not None
    result = await get_product_notes_and_combination_status(db_session, real.title)
    assert result["status"] == "FOUND"
    assert result["title"] == real.title
    all_notes = result["mainNotes"] + result["supportingNotes"]
    assert all_notes == real.notesJson


@pytest.mark.asyncio
async def test_identifies_real_hybrid_and_its_component(db_session):
    hybrid = await db_session.scalar(select(ExistingCombination).where(ExistingCombination.type == "HYBRID").limit(1))
    assert hybrid is not None
    hybrid_result = await get_product_notes_and_combination_status(db_session, hybrid.title)
    assert hybrid_result["isHybrid"] is True
    assert hybrid_result["isTribrid"] is False
    assert hybrid_result["isQuadbrid"] is False

    component_title = hybrid.componentProductsJson[0]
    component_result = await get_product_notes_and_combination_status(db_session, component_title)
    assert any(c["title"] == hybrid.title for c in component_result["appearsAsComponentIn"])


@pytest.mark.asyncio
async def test_returns_real_inspiration_metadata_when_present(db_session):
    title = f"Pytest Inspiration Product {time.time()}-{uuid.uuid4().hex[:8]}"
    now = utcnow()
    product = FragranceProduct(
        id=new_id(),
        title=title,
        normalizedTitle=normalize_product_name(title),
        notesJson=["Rose", "Musk"],
        isSingleInspiration=True,
        tagLine="Inspiration: Opera by Sospiro",
        inspirationName="Opera",
        inspirationBrand="Sospiro",
        createdAt=now,
        updatedAt=now,
    )
    db_session.add(product)
    await db_session.commit()
    try:
        result = await get_product_notes_and_combination_status(db_session, title)
        assert result["isSingleInspiration"] is True
        assert result["tagLine"] == "Inspiration: Opera by Sospiro"
        assert result["inspirationName"] == "Opera"
        assert result["inspirationBrand"] == "Sospiro"
    finally:
        await db_session.execute(delete(FragranceProduct).where(FragranceProduct.id == product.id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_returns_false_null_inspiration_metadata_when_absent(db_session):
    title = f"Pytest Non-Inspiration Product {time.time()}-{uuid.uuid4().hex[:8]}"
    now = utcnow()
    product = FragranceProduct(
        id=new_id(), title=title, normalizedTitle=normalize_product_name(title), notesJson=["Oud"],
        createdAt=now, updatedAt=now,
    )
    db_session.add(product)
    await db_session.commit()
    try:
        result = await get_product_notes_and_combination_status(db_session, title)
        assert result["isSingleInspiration"] is False
        assert result["tagLine"] is None
        assert result["inspirationName"] is None
        assert result["inspirationBrand"] is None
    finally:
        await db_session.execute(delete(FragranceProduct).where(FragranceProduct.id == product.id))
        await db_session.commit()
