import time
import uuid

import pytest
from sqlalchemy import delete, select

from app.db.ids import new_id
from app.db.models import FragranceProduct, OdooOilMapping
from app.db.time import utcnow
from app.services import odoo_inventory


# Phase 7: these tests need a product / hybrid / order-history row to exist, not the real catalog.
pytestmark = pytest.mark.usefixtures("synthetic_catalog")

def _mock_response(products, success=True, ok=True):
    return {"ok": ok, "status": 200, "json": {"success": success, "products": products}}


async def _create_mapping(session, **overrides):
    mapping = OdooOilMapping(
        id=new_id(),
        fragranceProductId=overrides.pop("fragranceProductId", f"pytest-odoo-{time.time()}-{uuid.uuid4().hex[:6]}"),
        odooSku=overrides.pop("odooSku", "OIL-PYTEST-SKU"),
        active=overrides.pop("active", True),
        createdAt=utcnow(), updatedAt=utcnow(),
        **overrides,
    )
    session.add(mapping)
    await session.commit()
    return mapping


@pytest.fixture(autouse=True)
def _clear_cache():
    odoo_inventory.clear_odoo_inventory_cache_for_testing()
    yield
    odoo_inventory.clear_odoo_inventory_cache_for_testing()


@pytest.mark.asyncio
async def test_missing_when_no_mapping_row(db_session):
    result = await odoo_inventory.get_oil_inventory_for_product(db_session, f"pytest-no-mapping-{time.time()}")
    assert result["mappingStatus"] == "MISSING"
    assert result["found"] is False


@pytest.mark.asyncio
async def test_missing_when_mapping_inactive(db_session):
    mapping = await _create_mapping(db_session, active=False)
    try:
        result = await odoo_inventory.get_oil_inventory_for_product(db_session, mapping.fragranceProductId)
        assert result["mappingStatus"] == "MISSING"
    finally:
        await db_session.execute(delete(OdooOilMapping).where(OdooOilMapping.id == mapping.id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_connected_uses_on_hand_qty(db_session, monkeypatch):
    mapping = await _create_mapping(db_session)
    try:
        async def _get_inventory_by_skus(skus):
            return _mock_response([{"name": "Pytest Oil", "default_code": "OIL-PYTEST-SKU", "on_hand_qty": 400}])

        monkeypatch.setattr(odoo_inventory, "get_inventory_by_skus", _get_inventory_by_skus)
        result = await odoo_inventory.get_oil_inventory_for_product(db_session, mapping.fragranceProductId)
        assert result["mappingStatus"] == "CONNECTED"
        assert result["availableOilMl"] == 400
        assert result["odooSku"] == "OIL-PYTEST-SKU"
    finally:
        await db_session.execute(delete(OdooOilMapping).where(OdooOilMapping.id == mapping.id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_sku_not_found(db_session, monkeypatch):
    mapping = await _create_mapping(db_session)
    try:
        async def _get_inventory_by_skus(skus):
            return _mock_response([{"name": "Some Other Oil", "default_code": "OIL-SOMETHING-ELSE", "on_hand_qty": 10}])

        monkeypatch.setattr(odoo_inventory, "get_inventory_by_skus", _get_inventory_by_skus)
        result = await odoo_inventory.get_oil_inventory_for_product(db_session, mapping.fragranceProductId)
        assert result["mappingStatus"] == "SKU_NOT_FOUND"
    finally:
        await db_session.execute(delete(OdooOilMapping).where(OdooOilMapping.id == mapping.id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_lookup_failed_unreachable(db_session, monkeypatch):
    mapping = await _create_mapping(db_session)
    try:
        async def _get_inventory_by_skus(skus):
            return {"ok": False, "status": None, "error": "ECONNREFUSED"}

        monkeypatch.setattr(odoo_inventory, "get_inventory_by_skus", _get_inventory_by_skus)
        result = await odoo_inventory.get_oil_inventory_for_product(db_session, mapping.fragranceProductId)
        assert result["mappingStatus"] == "LOOKUP_FAILED"
    finally:
        await db_session.execute(delete(OdooOilMapping).where(OdooOilMapping.id == mapping.id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_lookup_failed_malformed_response(db_session, monkeypatch):
    mapping = await _create_mapping(db_session)
    try:
        async def _get_inventory_by_skus(skus):
            return {"ok": False, "status": 404, "json": None}

        monkeypatch.setattr(odoo_inventory, "get_inventory_by_skus", _get_inventory_by_skus)
        result = await odoo_inventory.get_oil_inventory_for_product(db_session, mapping.fragranceProductId)
        assert result["mappingStatus"] == "LOOKUP_FAILED"
    finally:
        await db_session.execute(delete(OdooOilMapping).where(OdooOilMapping.id == mapping.id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_lookup_failed_when_success_not_true(db_session, monkeypatch):
    mapping = await _create_mapping(db_session)
    try:
        async def _get_inventory_by_skus(skus):
            return {"ok": True, "status": 200, "json": {"success": False}}

        monkeypatch.setattr(odoo_inventory, "get_inventory_by_skus", _get_inventory_by_skus)
        result = await odoo_inventory.get_oil_inventory_for_product(db_session, mapping.fragranceProductId)
        assert result["mappingStatus"] == "LOOKUP_FAILED"
    finally:
        await db_session.execute(delete(OdooOilMapping).where(OdooOilMapping.id == mapping.id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_batches_multiple_components_into_one_request(db_session, monkeypatch):
    products = (await db_session.execute(select(FragranceProduct.id, FragranceProduct.title).limit(2))).all()
    assert len(products) == 2
    mappings = []
    for i, (product_id, _title) in enumerate(products):
        existing = await db_session.scalar(select(OdooOilMapping).where(OdooOilMapping.fragranceProductId == product_id))
        if existing:
            mappings.append(("existing", existing, existing.odooSku, existing.active))
            existing.odooSku = f"OIL-PYTEST-BATCH-{i}"
            existing.active = True
        else:
            m = OdooOilMapping(id=new_id(), fragranceProductId=product_id, odooSku=f"OIL-PYTEST-BATCH-{i}", active=True, createdAt=utcnow(), updatedAt=utcnow())
            db_session.add(m)
            mappings.append(("new", m, None, None))
    await db_session.commit()

    call_count = 0

    async def _get_inventory_by_skus(skus):
        nonlocal call_count
        call_count += 1
        return _mock_response([
            {"name": "A", "default_code": "OIL-PYTEST-BATCH-0", "on_hand_qty": 1830},
            {"name": "B", "default_code": "OIL-PYTEST-BATCH-1", "on_hand_qty": 0},
        ])

    monkeypatch.setattr(odoo_inventory, "get_inventory_by_skus", _get_inventory_by_skus)
    try:
        titles = [t for _, t in products]
        result = await odoo_inventory.get_oil_inventory_for_product_titles(db_session, titles)
        assert result["requestCount"] == 1
        assert call_count == 1
        assert sorted(result["skusQueried"]) == ["OIL-PYTEST-BATCH-0", "OIL-PYTEST-BATCH-1"]
        assert result["results"][titles[0]]["availableOilMl"] == 1830
        assert result["results"][titles[1]]["availableOilMl"] == 0
    finally:
        for kind, m, orig_sku, orig_active in mappings:
            if kind == "existing":
                m.odooSku = orig_sku
                m.active = orig_active
            else:
                await db_session.execute(delete(OdooOilMapping).where(OdooOilMapping.id == m.id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_zero_requests_when_nothing_needs_lookup(db_session, monkeypatch):
    called = False

    async def _get_inventory_by_skus(skus):
        nonlocal called
        called = True
        return _mock_response([])

    monkeypatch.setattr(odoo_inventory, "get_inventory_by_skus", _get_inventory_by_skus)
    result = await odoo_inventory.get_oil_inventory_for_product_titles(db_session, [f"Definitely Not A Real Product {time.time()}"])
    assert result["requestCount"] == 0
    assert called is False


def test_classify_stock_status_thresholds():
    assert odoo_inventory.classify_stock_status(21) == "IN_STOCK"
    assert odoo_inventory.classify_stock_status(6) == "LOW_STOCK"
    assert odoo_inventory.classify_stock_status(1) == "CRITICAL"
    assert odoo_inventory.classify_stock_status(0) == "OUT_OF_STOCK"
    assert odoo_inventory.classify_stock_status(None) == "UNKNOWN"
