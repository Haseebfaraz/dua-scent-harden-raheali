import logging
import time
import uuid

import pytest
from sqlalchemy import delete, select

from app.db.ids import new_id
from app.db.models import FragranceProduct, FragranceRecommendation, OdooOilMapping
from app.db.time import utcnow
from app.services import odoo_inventory
from app.services.inventory_snapshot import get_inventory_snapshot, save_inventory_snapshot
from app.services.recommendation_confirmation import save_recommendation


# Phase 7: these tests need a product / hybrid / order-history row to exist, not the real catalog.
pytestmark = pytest.mark.usefixtures("synthetic_catalog")

async def _real_combo(session):
    products = (await session.execute(select(FragranceProduct.title, FragranceProduct.notesJson).limit(2))).all()
    (title_a, notes_a), (title_b, notes_b) = products
    return {
        "type": "HYBRID",
        "canonicalKey": f"pytest-invsnap-key-{time.time()}-{uuid.uuid4().hex[:8]}",
        "internalProducts": [
            {"title": title_a, "contribution": "Freshness", "notes": notes_a},
            {"title": title_b, "contribution": "Sweetness", "notes": notes_b},
        ],
        "recommendedRatio": [
            {"productTitle": title_a, "ratioPercent": 50},
            {"productTitle": title_b, "ratioPercent": 50},
        ],
        "customerFacingName": "Pytest Blend",
    }


async def _make_recommendation(session, label):
    combination = await _real_combo(session)
    conversation_id = f"pytest-invsnap-{label}-{time.time()}-{uuid.uuid4().hex[:8]}"
    recommendation_id = await save_recommendation(session, conversation_id=conversation_id, profile={"likes": [], "dislikes": []}, combination=combination)
    return recommendation_id, combination


async def _cleanup_recommendation(session, recommendation_id):
    await session.execute(delete(FragranceRecommendation).where(FragranceRecommendation.id == recommendation_id))
    await session.commit()


@pytest.mark.asyncio
async def test_persists_and_reads_back_unchanged(db_session):
    recommendation_id, _ = await _make_recommendation(db_session, "basic")
    try:
        checked_at = utcnow().isoformat()
        await save_inventory_snapshot(
            db_session, recommendation_id=recommendation_id, inventory_validated=True, buildable=True,
            checked_at=checked_at, oil_total_ml=13, alcohol_ml=21, request_status="ok",
            max_buildable_bottles=42, limiting_sku="DUA-OPERA_Oil",
            components=[
                {
                    "fragranceProductId": "fp-1", "productTitle": "The Opera", "odooSku": "DUA-OPERA_Oil",
                    "ratioPercent": 50, "requiredOilMl": 6.5, "onHandQty": 300, "mappingStatus": "CONNECTED",
                    "sufficient": True, "maxBuildableBottlesForComponent": 46,
                },
                {
                    "fragranceProductId": None, "productTitle": "Water of Arabia", "odooSku": None,
                    "ratioPercent": 50, "requiredOilMl": 6.5, "onHandQty": None, "mappingStatus": "MISSING",
                    "sufficient": None, "maxBuildableBottlesForComponent": None,
                },
            ],
        )

        snapshot = await get_inventory_snapshot(db_session, recommendation_id)
        assert snapshot.inventoryValidated is True
        assert snapshot.buildable is True
        assert snapshot.oilTotalMl == 13
        assert snapshot.alcoholMl == 21
        assert snapshot.maxBuildableBottles == 42
        assert snapshot.limitingSku == "DUA-OPERA_Oil"
        assert snapshot.requestStatus == "ok"
        assert len(snapshot.components) == 2

        opera = next(c for c in snapshot.components if c.productTitle == "The Opera")
        assert opera.odooSku == "DUA-OPERA_Oil"
        assert opera.requiredOilMl == 6.5
        assert opera.onHandQty == 300
        assert opera.sufficient is True
        assert opera.maxBuildableBottlesForComponent == 46

        water = next(c for c in snapshot.components if c.productTitle == "Water of Arabia")
        assert water.mappingStatus == "MISSING"
        assert water.sufficient is None
        assert water.onHandQty is None
    finally:
        await _cleanup_recommendation(db_session, recommendation_id)


@pytest.mark.asyncio
async def test_never_persists_extra_secret_field(db_session):
    recommendation_id, _ = await _make_recommendation(db_session, "secret-guard")
    try:
        await save_inventory_snapshot(
            db_session, recommendation_id=recommendation_id, inventory_validated=False, buildable=True,
            checked_at=utcnow().isoformat(), oil_total_ml=13, alcohol_ml=21, request_status="ok",
            max_buildable_bottles=None, limiting_sku=None,
            components=[{
                "fragranceProductId": None, "productTitle": "The Opera", "odooSku": "DUA-OPERA_Oil",
                "ratioPercent": 50, "requiredOilMl": 6.5, "onHandQty": None, "mappingStatus": "LOOKUP_FAILED",
                "sufficient": None, "maxBuildableBottlesForComponent": None,
                # Not a declared field -- explicit key access in save_inventory_snapshot means this
                # must never land in the DB.
                "_rawOdooRequest": {"headers": {"Authorization": "Bearer f4f0ba800d6e298a616c5dfb25f2f4876957f75a"}},
            }],
        )

        snapshot = await get_inventory_snapshot(db_session, recommendation_id)
        serialized = repr([(c.productTitle, c.odooSku, c.mappingStatus) for c in snapshot.components])
        assert "Bearer" not in serialized
        assert "Authorization" not in serialized
        assert "f4f0ba800d6e298a616c5dfb25f2f4876957f75a" not in serialized
    finally:
        await _cleanup_recommendation(db_session, recommendation_id)


@pytest.mark.asyncio
async def test_enforces_one_snapshot_per_recommendation(db_session):
    recommendation_id, _ = await _make_recommendation(db_session, "immutable")
    try:
        args = dict(
            recommendation_id=recommendation_id, inventory_validated=True, buildable=True, checked_at=utcnow().isoformat(),
            oil_total_ml=13, alcohol_ml=21, request_status="ok", max_buildable_bottles=10, limiting_sku=None, components=[],
        )
        await save_inventory_snapshot(db_session, **args)
        with pytest.raises(Exception):
            await save_inventory_snapshot(db_session, **{**args, "oil_total_ml": 999})
        await db_session.rollback()

        snapshot = await get_inventory_snapshot(db_session, recommendation_id)
        assert snapshot.oilTotalMl == 13
    finally:
        await _cleanup_recommendation(db_session, recommendation_id)


@pytest.mark.asyncio
async def test_get_inventory_snapshot_null_when_never_checked(db_session):
    recommendation_id, _ = await _make_recommendation(db_session, "no-snapshot")
    try:
        assert await get_inventory_snapshot(db_session, recommendation_id) is None
    finally:
        await _cleanup_recommendation(db_session, recommendation_id)


async def _map_real_product(session, title, sku):
    product = await session.scalar(select(FragranceProduct).where(FragranceProduct.title == title))
    existing = await session.scalar(select(OdooOilMapping).where(OdooOilMapping.fragranceProductId == product.id))
    if existing:
        original = {"odooSku": existing.odooSku, "active": existing.active}
        existing.odooSku = sku
        existing.active = True
    else:
        original = None
        session.add(OdooOilMapping(id=new_id(), fragranceProductId=product.id, odooSku=sku, active=True, createdAt=utcnow(), updatedAt=utcnow()))
    await session.commit()
    return product, original


@pytest.mark.asyncio
async def test_evaluate_candidate_inventory_integrates_with_save_snapshot(db_session, monkeypatch, caplog):
    products = (await db_session.execute(select(FragranceProduct.title).limit(2))).all()
    title_a, title_b = products[0][0], products[1][0]
    _, original_a = await _map_real_product(db_session, title_a, "OIL-PYTEST-SNAP-A")
    _, original_b = await _map_real_product(db_session, title_b, "OIL-PYTEST-SNAP-B")

    async def _get_inventory_by_skus(skus):
        return {"ok": True, "status": 200, "json": {"success": True, "products": [
            {"name": "A", "default_code": "OIL-PYTEST-SNAP-A", "on_hand_qty": 300},
            {"name": "B", "default_code": "OIL-PYTEST-SNAP-B", "on_hand_qty": 2},
        ]}}

    monkeypatch.setattr(odoo_inventory, "get_inventory_by_skus", _get_inventory_by_skus)
    odoo_inventory.clear_odoo_inventory_cache_for_testing()

    recommendation_id = None
    try:
        conversation_id = f"pytest-invsnap-integration-{time.time()}"
        combination = {
            "type": "HYBRID",
            "canonicalKey": f"pytest-invsnap-integration-key-{time.time()}",
            "internalProducts": [
                {"title": title_a, "contribution": "Freshness", "notes": ["x"]},
                {"title": title_b, "contribution": "Sweetness", "notes": ["y"]},
            ],
            "recommendedRatio": [{"productTitle": title_a, "ratioPercent": 50}, {"productTitle": title_b, "ratioPercent": 50}],
            "customerFacingName": "Pytest Integration Blend",
        }
        recommendation_id = await save_recommendation(db_session, conversation_id=conversation_id, profile={"likes": [], "dislikes": []}, combination=combination)

        candidate = {"recommendationId": recommendation_id, "recommendedRatio": combination["recommendedRatio"]}
        with caplog.at_level(logging.INFO):
            inventory = await odoo_inventory.evaluate_candidate_inventory(db_session, candidate)

        await save_inventory_snapshot(
            db_session, recommendation_id=recommendation_id, inventory_validated=inventory["inventoryValidated"],
            buildable=inventory["buildable"], checked_at=utcnow().isoformat(), oil_total_ml=inventory["oilTotalMl"],
            alcohol_ml=inventory["alcoholMl"], request_status=inventory["status"],
            max_buildable_bottles=inventory["maxBuildableBottles"], limiting_sku=inventory["limitingSku"],
            components=inventory["components"],
        )

        snapshot = await get_inventory_snapshot(db_session, recommendation_id)
        assert snapshot.buildable == inventory["buildable"]
        assert snapshot.buildable is False  # B only has 2ml, needs 6.5ml
        assert snapshot.maxBuildableBottles == inventory["maxBuildableBottles"]
        assert snapshot.limitingSku == "OIL-PYTEST-SNAP-B"

        b_component = next(c for c in snapshot.components if c.odooSku == "OIL-PYTEST-SNAP-B")
        assert b_component.onHandQty == 2
        assert b_component.requiredOilMl == pytest.approx(6.5)
        assert b_component.sufficient is False

        # Real SKU/ratio/requiredOilMl must be logged; no secret ever appears.
        log_text = "\n".join(r.getMessage() for r in caplog.records)
        # Phase 6 removed item codes and quantities from inventory logs (privacy); the snapshot row
        # is where that detail lives now. The log carries counts, and never a credential.
        assert "ODOO_INVENTORY_REQUEST" in log_text and "ODOO_INVENTORY_RESPONSE" in log_text
        assert "OIL-PYTEST-SNAP-A" not in log_text and "requiredOilMl" not in log_text
        assert "Bearer" not in log_text
        assert "Authorization" not in log_text
    finally:
        if recommendation_id:
            await _cleanup_recommendation(db_session, recommendation_id)
        if original_a:
            mapping = await db_session.scalar(select(OdooOilMapping).where(OdooOilMapping.odooSku == "OIL-PYTEST-SNAP-A"))
            if mapping:
                mapping.odooSku, mapping.active = original_a["odooSku"], original_a["active"]
        else:
            await db_session.execute(delete(OdooOilMapping).where(OdooOilMapping.odooSku == "OIL-PYTEST-SNAP-A"))
        if original_b:
            mapping = await db_session.scalar(select(OdooOilMapping).where(OdooOilMapping.odooSku == "OIL-PYTEST-SNAP-B"))
            if mapping:
                mapping.odooSku, mapping.active = original_b["odooSku"], original_b["active"]
        else:
            await db_session.execute(delete(OdooOilMapping).where(OdooOilMapping.odooSku == "OIL-PYTEST-SNAP-B"))
        await db_session.commit()
        odoo_inventory.clear_odoo_inventory_cache_for_testing()
