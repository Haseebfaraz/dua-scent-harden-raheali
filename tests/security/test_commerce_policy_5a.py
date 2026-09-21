# ruff: noqa: F811  -- pytest fixtures imported from the Phase 5 suite are re-declared as test parameters by design
"""Phase 5A regressions.

1. A stock OBSERVATION is not a commerce APPROVAL: every fact the policy needs must be known.
2. The conservative requirement bound rests on a declared manufacturing contract, or it blocks.
3. A refused concurrent request changes nothing (the draft is written inside the lock).
4. Nothing is purchasable before its price is set and read back; failures are described honestly.
5. A crash at any boundary never leads to a blind second creation.
6. Deterministic tests cannot reach the network, by any client or import path.

Synthetic fixtures, mocked Odoo and Shopify, disposable local PostgreSQL. The real policy code runs
in every test here; nothing mocks the gate itself to success.
"""

import asyncio
import json
import os
import socket
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings, settings
from app.db.models import FragranceRecommendation
from app.db.session import SessionLocal
from app.db.time import utcnow
from app.fragrance.formulas import FINISHED_BOTTLE_ML, MAX_OIL_ML
from app.integrations import odoo_client
from app.main import app
from app.services import build_commerce, commerce_inventory, odoo_inventory
from app.services.build_commerce import BuildOperationInProgress, BuildPendingReview, execute_build_commerce, save_recreate_draft
from app.services.commerce_inventory import (
    COMMERCE_FAILURES, InventoryNotVerified, InventoryState, ReportedStock, RequirementsUnknown, compute_build_requirements,
    manufacturing_bound_ml, source_contract_gaps, verify_build_inventory,
)
from app.services.recommendation_confirmation import mark_recommendation_draft
from app.shopify import builds
from tests.conftest import ExternalNetworkBlocked
from tests.security.test_commerce_inventory import (  # noqa: F401 -- fixtures are re-used on purpose
    LOCATION, RATIOS, SHOP, Odoo, ShopifyWrites, _env, _preview_post, _rec, _token_for, _two_component_build, catalog, odoo,
)

OTHER_RATIOS = {"top": 10, "middle": 10, "base": 80}


async def _decide(rec, ratios=RATIOS):
    async with SessionLocal() as session:
        return (await verify_build_inventory(session, recommendation=rec, ratios=ratios))[1]


# ===========================================================================
# 1. Observation versus approval
# ===========================================================================

async def test_complete_synthetic_contract_is_the_only_thing_that_satisfies_policy(catalog, odoo):
    rec = await _two_component_build(catalog, odoo)
    decision = await _decide(rec)
    assert decision.state is InventoryState.POLICY_SATISFIED and source_contract_gaps() == []
    assert decision.observation.reported is ReportedStock.SUFFICIENT_REPORTED
    assert decision.observation.quantity_field == "available_qty" and decision.observation.location_echoed is True


@pytest.mark.parametrize("setting, value, reason", [
    ("odoo_inventory_url", "", commerce_inventory.INTEGRATION_NOT_CONFIGURED),
    ("odoo_inventory_url", "http://plain-http.synthetic.invalid/x", commerce_inventory.INTEGRATION_NOT_CONFIGURED),
    ("odoo_inventory_location_scope", "", commerce_inventory.SOURCE_SCOPE_UNDECLARED),
    ("odoo_inventory_location_scope", "   ", commerce_inventory.SOURCE_SCOPE_UNDECLARED),
    ("odoo_inventory_quantity_semantics", "", commerce_inventory.RESERVATION_SEMANTICS_UNDECLARED),
    ("odoo_inventory_quantity_semantics", "probably fine", commerce_inventory.RESERVATION_SEMANTICS_UNDECLARED),
    ("odoo_inventory_quantity_semantics", "ON_HAND_INCLUDES_RESERVED", commerce_inventory.RESERVATION_SEMANTICS_INSUFFICIENT),
    ("manufacturing_max_oil_ml_per_bottle", None, commerce_inventory.MANUFACTURING_CONTRACT_MISSING),
], ids=["no_integration", "not_https", "location_undeclared", "location_blank", "semantics_undeclared", "semantics_nonsense", "on_hand_only_declared", "no_manufacturing_contract"])
async def test_each_undeclared_fact_blocks_with_zero_lookups_and_zero_writes(catalog, odoo, monkeypatch, setting, value, reason):
    """Plenty of stock is on hand in every one of these cases. It does not matter."""
    rec = await _two_component_build(catalog, odoo, stock=(99999.0, 99999.0))
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    monkeypatch.setattr(settings, setting, value)
    decision = await _decide(rec)
    assert decision.state is InventoryState.UNCONFIRMED and reason in decision.reasons
    assert decision.observation.reported is ReportedStock.NOT_OBSERVED
    assert odoo.calls == []  # no necessary fact, no query: a number without the facts could only be misread
    async with SessionLocal() as session:
        with pytest.raises(InventoryNotVerified) as err:
            await builds.create_shopify_build_product(session, SHOP, recommendation=rec, custom_name="My Blend", ratios=RATIOS, customer_name=None, customer_email=None)
    assert err.value.state is InventoryState.UNCONFIRMED and shopify.writes == []


async def test_recording_millilitres_alone_never_approves(catalog, odoo, monkeypatch):
    """The Phase 5 behaviour: unit recorded + positive on-hand quantity = VERIFIED_AVAILABLE.
    Location and reservation semantics were unknown. That is now UNCONFIRMED."""
    rec = await _two_component_build(catalog, odoo, stock=(99999.0, 99999.0))
    monkeypatch.setattr(settings, "odoo_inventory_location_scope", "")
    monkeypatch.setattr(settings, "odoo_inventory_quantity_semantics", "")
    decision = await _decide(rec)
    assert decision.state is InventoryState.UNCONFIRMED
    assert {commerce_inventory.SOURCE_SCOPE_UNDECLARED, commerce_inventory.RESERVATION_SEMANTICS_UNDECLARED} <= set(decision.reasons)


@pytest.mark.parametrize("body_patch, reason", [
    (lambda b: b.pop("location"), commerce_inventory.SOURCE_SCOPE_UNCONFIRMED),
    (lambda b: b.update(location="ANOTHER/Warehouse"), commerce_inventory.SOURCE_SCOPE_UNCONFIRMED),
    (lambda b: b.update(location=LOCATION.lower()), commerce_inventory.SOURCE_SCOPE_UNCONFIRMED),
    (lambda b: b.update(location=["SYNTH/Stock"]), commerce_inventory.SOURCE_SCOPE_UNCONFIRMED),
    (lambda b: [r.pop("available_qty") for r in b["products"]], commerce_inventory.RESERVATION_SEMANTICS_INSUFFICIENT),
    (lambda b: b["products"][0].update(uom="L"), commerce_inventory.UNIT_INCONSISTENT),
    (lambda b: b["products"][0].update(uom=None), commerce_inventory.UNIT_INCONSISTENT),
    (lambda b: b["products"][0].update(uom="units"), commerce_inventory.UNIT_INCONSISTENT),
], ids=["location_not_echoed", "other_location", "location_case_differs", "location_not_a_string", "only_on_hand_reported", "unit_litres", "unit_null", "unit_units"])
async def test_a_declaration_the_source_does_not_back_up_is_unconfirmed_even_when_stock_is_reported(catalog, odoo, monkeypatch, body_patch, reason):
    rec = await _two_component_build(catalog, odoo, stock=(99999.0, 99999.0))
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)

    def _respond(items):
        body = {"success": True, "location": LOCATION, "products": [{"default_code": s, "uom": "ml", "available_qty": 99999.0, "on_hand_qty": 99999.0} for s in items]}
        body_patch(body)
        return {"ok": True, "status": 200, "json": body}

    odoo.response = _respond
    decision = await _decide(rec)
    assert decision.state is InventoryState.UNCONFIRMED and reason in decision.reasons
    async with SessionLocal() as session:
        with pytest.raises(InventoryNotVerified):
            await builds.reprice_existing_build(session, SHOP, recommendation=await _saved(rec), ratios=OTHER_RATIOS, name=None)
    assert shopify.writes == []


async def _saved(rec):
    async with SessionLocal() as session:
        stored = await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == rec.id))
        stored.shopifyProductId, stored.buildStatus = "gid://shopify/Product/555", "saved"
        await session.commit()
        return stored


async def test_observation_is_recorded_separately_from_the_decision(catalog, odoo):
    rec = await _two_component_build(catalog, odoo, stock=(99999.0, 99999.0))
    odoo.response = lambda items: {"ok": True, "json": {"success": True, "location": "ELSEWHERE", "products": [{"default_code": s, "uom": "ml", "available_qty": 99999.0} for s in items]}}
    decision = await _decide(rec)
    # The source DID report plenty ...
    assert decision.observation.reported is ReportedStock.SUFFICIENT_REPORTED and decision.observation.location_echoed is False
    # ... and the policy still says no.
    assert decision.state is InventoryState.UNCONFIRMED and decision.authorizes(decision.operation_fingerprint)[0] is False


def test_no_setting_is_a_bypass():
    names = [n.lower() for n in Settings.model_fields]
    assert not [n for n in names if any(w in n for w in ("inventory_approved", "skip_inventory", "bypass", "allow_unknown", "fail_open", "inventory_optional", "disable_inventory"))]
    # The declared facts are descriptive; none of them is boolean.
    for name in ("odoo_inventory_location_scope", "odoo_inventory_quantity_semantics", "manufacturing_max_oil_ml_per_bottle"):
        assert Settings.model_fields[name].annotation is not bool


def test_source_contract_has_no_defaults_so_real_commerce_starts_blocked():
    fresh = Settings(_env_file=None, database_url="postgresql://u:p@127.0.0.1:1/x", openai_api_key="x", openai_model="x")
    assert fresh.odoo_inventory_url == "" and fresh.odoo_ping_url == ""
    assert fresh.odoo_inventory_location_scope == "" and fresh.odoo_inventory_quantity_semantics == ""
    assert fresh.manufacturing_max_oil_ml_per_bottle is None


# ===========================================================================
# 2. The conservative bound
# ===========================================================================

def test_bound_is_unknown_without_a_declared_manufacturing_contract(monkeypatch):
    monkeypatch.setattr(settings, "manufacturing_max_oil_ml_per_bottle", None)
    with pytest.raises(RequirementsUnknown) as err:
        compute_build_requirements(_rec(), RATIOS)
    assert err.value.reason == commerce_inventory.MANUFACTURING_CONTRACT_MISSING


@pytest.mark.parametrize("declared", [0, -14, 13.99, MAX_OIL_ML - 0.01, FINISHED_BOTTLE_ML + 0.01, 1000, float("nan"), float("inf"), True])
def test_a_contract_that_contradicts_the_repository_formula_is_rejected(monkeypatch, declared):
    """Below the formula's own maximum oil volume, above the bottle, or not a number."""
    monkeypatch.setattr(settings, "manufacturing_max_oil_ml_per_bottle", declared)
    with pytest.raises(RequirementsUnknown) as err:
        manufacturing_bound_ml()
    assert err.value.reason == commerce_inventory.MANUFACTURING_CONTRACT_INVALID


@pytest.mark.parametrize("declared, expected", [(14, "14.00"), (14.0, "14.00"), (15.5, "15.50"), (16.25, "16.25"), (34, "34.00")])
def test_supported_bound_uses_the_declared_value_with_loss_allowance(monkeypatch, declared, expected):
    monkeypatch.setattr(settings, "manufacturing_max_oil_ml_per_bottle", declared)
    assert compute_build_requirements(_rec(), RATIOS).required_ml_per_item == Decimal(expected)


def test_the_invariant_the_bound_relies_on_holds_for_the_repository_formula(monkeypatch):
    """Given the premise 'one bottle draws at most M ml of oil in total', no single component can
    need more than M. Checked against the formula this repository owns, for every valid split."""
    from app.fragrance.formulas import build_production_formula

    monkeypatch.setattr(settings, "manufacturing_max_oil_ml_per_bottle", float(MAX_OIL_ML))
    bound = float(manufacturing_bound_ml())
    for oil_total in (12, 13, 14):
        for a in range(1, 100):
            formula = build_production_formula([{"productTitle": "A", "ratioPercent": a}, {"productTitle": "B", "ratioPercent": 100 - a}], oil_total_ml=oil_total)
            assert all(c["requiredOilMl"] <= bound for c in formula["components"])
            assert sum(c["requiredOilMl"] for c in formula["components"]) <= bound + 0.011  # rounding to 0.01 ml per component


async def test_bound_changes_the_decision_and_the_fingerprint(catalog, odoo, monkeypatch):
    rec = await _two_component_build(catalog, odoo, stock=(15.0, 15.0))
    first = await _decide(rec)
    assert first.state is InventoryState.POLICY_SATISFIED  # 15 >= 14
    monkeypatch.setattr(settings, "manufacturing_max_oil_ml_per_bottle", 16.0)  # contract now includes a loss allowance
    second = await _decide(rec)
    assert second.state is InventoryState.INSUFFICIENT  # 15 < 16
    assert second.operation_fingerprint != first.operation_fingerprint
    assert first.authorizes(second.operation_fingerprint)[0] is False


async def test_repeated_components_sharing_one_item_are_one_demand_under_the_bound(catalog, odoo):
    shared = f"FAKE-OIL-{catalog.tag}-shared"
    for n in (1, 2, 3):
        await catalog.add_product(n, sku=shared)
    rec = await catalog.add_recommendation([(1, 50), (2, 30), (3, 20)])
    odoo.stock = {shared: float(MAX_OIL_ML)}
    decision = await _decide(rec)
    assert odoo.calls == [[shared]] and decision.observation.item_count == 1
    assert decision.state is InventoryState.POLICY_SATISFIED  # all three together cannot exceed the bottle's total oil
    odoo.stock[shared] = float(MAX_OIL_ML) - 0.01
    assert (await _decide(rec)).state is InventoryState.INSUFFICIENT


def test_documentation_no_longer_claims_the_bound_never_approves_an_unmakeable_build():
    text = open("docs/INVENTORY_COMMERCE_SECURITY.md").read()
    assert "It never approves one it could not." not in text  # the claim itself is gone (the correction note quotes it as withdrawn)
    assert "That does not follow" in text
    assert "sufficient consumption condition" in text.lower()


# ===========================================================================
# 3. Binding to the exact operation, controlled clock
# ===========================================================================

async def test_decision_is_bound_to_mapping_location_and_semantics(catalog, odoo, monkeypatch):
    rec = await _two_component_build(catalog, odoo)
    base = await _decide(rec)
    assert base.state is InventoryState.POLICY_SATISFIED

    monkeypatch.setattr(settings, "odoo_inventory_location_scope", "ANOTHER/Warehouse")
    moved = await _decide(rec)
    assert moved.operation_fingerprint != base.operation_fingerprint and base.authorizes(moved.operation_fingerprint)[0] is False
    monkeypatch.setattr(settings, "odoo_inventory_location_scope", LOCATION)

    from app.db.models import OdooOilMapping
    async with SessionLocal() as session:  # component 2 is re-mapped to a different physical item
        mapping = await session.scalar(select(OdooOilMapping).where(OdooOilMapping.odooSku == catalog.sku(2)))
        mapping.odooSku = catalog.sku(2) + "-B"
        await session.commit()
    odoo.stock[catalog.sku(2) + "-B"] = 100.0
    remapped = await _decide(rec)
    assert remapped.operation_fingerprint != base.operation_fingerprint and base.authorizes(remapped.operation_fingerprint)[0] is False


async def test_expiry_with_a_controlled_clock(catalog, odoo, monkeypatch):
    rec = await _two_component_build(catalog, odoo)
    clock = {"now": utcnow()}
    monkeypatch.setattr(commerce_inventory, "_now", lambda: clock["now"])
    decision = await _decide(rec)
    window = settings.commerce_inventory_max_age_seconds
    clock["now"] += timedelta(seconds=window)
    assert decision.authorizes(decision.operation_fingerprint) == (True, None)  # exactly at the boundary
    clock["now"] += timedelta(seconds=1)
    assert decision.authorizes(decision.operation_fingerprint) == (False, commerce_inventory.STALE)


async def test_evidence_that_expires_mid_operation_is_reverified_before_activation_not_silently_reused(catalog, odoo, monkeypatch):
    rec = await _two_component_build(catalog, odoo)
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    clock = {"now": utcnow()}
    monkeypatch.setattr(commerce_inventory, "_now", lambda: clock["now"])

    async def _slow_price(*a, **kw):  # the price step "takes" longer than the freshness window
        shopify.calls.append("set_variant_price")
        clock["now"] += timedelta(seconds=settings.commerce_inventory_max_age_seconds + 5)

    monkeypatch.setattr(builds, "set_variant_price", _slow_price)
    async with SessionLocal() as session:
        await builds.create_shopify_build_product(session, SHOP, recommendation=rec, custom_name="My Blend", ratios=RATIOS, customer_name=None, customer_email=None)
    assert len(odoo.calls) == 2 and "activate_product" in shopify.calls  # re-verified, still satisfied, then activated

    # Same again, but the stock is gone by the time of the re-verification: never activated.
    rec2 = await catalog.add_recommendation([(1, 60), (2, 40)])
    shopify2 = ShopifyWrites(monkeypatch, recommendation_id=rec2.id)

    async def _slow_price_then_stock_runs_out(*a, **kw):
        shopify2.calls.append("set_variant_price")
        clock["now"] += timedelta(seconds=settings.commerce_inventory_max_age_seconds + 5)
        odoo.stock = {catalog.sku(1): 0, catalog.sku(2): 0}

    monkeypatch.setattr(builds, "set_variant_price", _slow_price_then_stock_runs_out)
    async with SessionLocal() as session:
        with pytest.raises(builds.BuildWriteAmbiguous) as err:
            await builds.create_shopify_build_product(session, SHOP, recommendation=rec2, custom_name="My Blend", ratios=RATIOS, customer_name=None, customer_email=None)
    assert err.value.product_id and "activate_product" not in shopify2.calls and "publish_to_all_channels" not in shopify2.calls


def test_the_write_layer_accepts_no_decision_object_from_callers():
    import inspect

    for fn in (builds.create_shopify_build_product, builds.reprice_existing_build, execute_build_commerce):
        params = set(inspect.signature(fn).parameters)
        assert not params & {"decision", "verification", "inventory", "inventory_decision", "approved", "clearance"}, fn.__name__


# ===========================================================================
# 4. The draft-before-lock race
# ===========================================================================

async def test_refused_concurrent_request_changes_nothing_and_operation_a_is_internally_consistent(catalog, odoo, monkeypatch):
    rec = await _two_component_build(catalog, odoo)
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    seen: dict = {}
    inside, release = asyncio.Event(), asyncio.Event()
    real_require = builds.require_commerce_inventory

    async def _spy_require(session, *, recommendation, ratios, quantity=1):
        seen["verified_ratios"] = dict(ratios)
        return await real_require(session, recommendation=recommendation, ratios=ratios, quantity=quantity)

    async def _paused_create(session, shop, **kw):
        shopify.calls.append("create_product")
        seen["shopify_title"] = kw["title"]
        seen["shopify_options"] = [v["values"][0]["name"] for v in kw["product_options"]]
        inside.set()
        await release.wait()  # A is now paused INSIDE the protected operation
        return {"id": shopify.product_id, "handle": "custom-blend"}

    async def _price(session, shop, product_id, variant_id, price):
        shopify.calls.append("set_variant_price")
        seen["price"] = price

    monkeypatch.setattr(builds, "require_commerce_inventory", _spy_require)
    monkeypatch.setattr(builds, "create_product", _paused_create)
    monkeypatch.setattr(builds, "set_variant_price", _price)

    async def _request(ratios, name):
        async with SessionLocal() as session:
            return await execute_build_commerce(session, SHOP, recommendation_id=rec.id, ratios=ratios, name=name, save_draft=True)

    task_a = asyncio.create_task(_request(RATIOS, "Name From A"))
    await inside.wait()
    # B: a different, equally valid and authorized draft, while A is in progress.
    with pytest.raises(BuildOperationInProgress):
        await _request(OTHER_RATIOS, "Name From B")
    with pytest.raises(BuildOperationInProgress):
        async with SessionLocal() as session:
            await save_recreate_draft(session, recommendation_id=rec.id, name="Name From B", ratios=OTHER_RATIOS)
    async with SessionLocal() as session:
        during = await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == rec.id))
        assert (during.draftName, during.draftRatiosJson, during.buildStatus) == ("Name From A", RATIOS, "creating")  # B changed nothing
    release.set()
    result = await task_a

    # Everything about A corresponds to A's own authorized inputs.
    assert seen["verified_ratios"] == RATIOS and seen["shopify_title"] == "Name From A"
    assert all(f"({RATIOS[p]}%)" in option for p, option in zip(("top", "middle", "base"), seen["shopify_options"]))
    assert seen["price"] == "136.00" and result["created"] is True and shopify.calls.count("create_product") == 1
    async with SessionLocal() as session:
        after = await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == rec.id))
        assert (after.draftName, after.draftRatiosJson, after.buildStatus, after.shopifyProductId) == ("Name From A", RATIOS, "saved", shopify.product_id)


async def test_refused_preview_request_does_not_touch_the_draft_via_the_route(catalog, odoo, monkeypatch):
    rec = await _two_component_build(catalog, odoo)
    ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    token = await _token_for(rec)
    async with SessionLocal() as session:
        await mark_recommendation_draft(session, rec.id, name="Original", ratios=RATIOS)
    async with build_commerce.build_commerce_lock(rec.id):  # another operation holds the build
        loop = asyncio.get_running_loop()
        with TestClient(app) as client:
            payloads = await loop.run_in_executor(None, lambda: [
                _preview_post(client, rec, token, intent=intent, ratios=OTHER_RATIOS, name="Intruder").json() for intent in ("save_build", "add_to_cart", "recreate")])
    assert [p.get("code") for p in payloads] == ["build_in_progress"] * 3
    async with SessionLocal() as session:
        stored = await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == rec.id))
        assert (stored.draftName, stored.draftRatiosJson) == ("Original", RATIOS)


@pytest.mark.parametrize("marker", ["creating", "pending_review"])
async def test_status_markers_survive_every_refused_request_and_ordinary_draft_saving(catalog, odoo, monkeypatch, marker):
    rec = await _two_component_build(catalog, odoo, buildStatus=marker, draftName="Before")
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    token = await _token_for(rec)
    with TestClient(app) as client:
        for intent in ("save_build", "add_to_cart", "recreate"):
            assert _preview_post(client, rec, token, intent=intent, ratios=OTHER_RATIOS, name="After").json()["code"] == "build_pending_review"
    async with SessionLocal() as session:
        await mark_recommendation_draft(session, rec.id, name="Direct Draft Save", ratios=OTHER_RATIOS)
        stored = await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == rec.id))
        assert stored.buildStatus == marker  # never erased
    assert shopify.calls == [] and odoo.calls == []


async def test_other_recommendations_stay_independent_while_one_is_locked(catalog, odoo, monkeypatch):
    rec_a = await _two_component_build(catalog, odoo)
    rec_b = await catalog.add_recommendation([(1, 60), (2, 40)])
    ShopifyWrites(monkeypatch, recommendation_id=rec_b.id)
    async with build_commerce.build_commerce_lock(rec_a.id):
        async with SessionLocal() as session:
            assert (await execute_build_commerce(session, SHOP, recommendation_id=rec_b.id, ratios=RATIOS, name="B", save_draft=True))["created"] is True


# ===========================================================================
# 5. Publication and partial-failure safety
# ===========================================================================

def test_products_are_created_as_drafts():
    import inspect

    from app.shopify import products
    source = inspect.getsource(products.create_product)
    assert '"status": "DRAFT"' in source and '"status": "ACTIVE"' not in source


BOUNDARIES = {
    # step that fails        -> (writes that must NOT have happened, product id known?)
    "create_product": (("set_variant_price", "activate_product", "publish_to_all_channels"), False),
    "read:default_variant": (("set_variant_price", "activate_product", "publish_to_all_channels"), True),
    "set_variant_price": (("activate_product", "publish_to_all_channels"), True),
    "activate_product": (("publish_to_all_channels",), True),
}


@pytest.mark.parametrize("boundary", sorted(BOUNDARIES))
async def test_failure_at_each_boundary_never_activates_or_publishes_and_is_never_success(catalog, odoo, monkeypatch, boundary):
    rec = await _two_component_build(catalog, odoo)
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    forbidden, product_known = BOUNDARIES[boundary]
    if boundary == "read:default_variant":
        async def _boom(*a, **kw):
            shopify.calls.append("read:default_variant")
            raise TimeoutError("timed out")
        monkeypatch.setattr(builds, "get_default_variant_id", _boom)
    else:
        shopify.fail_on[boundary] = TimeoutError("timed out")
    token = await _token_for(rec)
    with TestClient(app) as client:
        payload = _preview_post(client, rec, token, intent="add_to_cart").json()
    assert payload["code"] == "build_pending_review" and "status" not in payload and "cartUrl" not in payload and "shopifyVariantId" not in payload
    for step in forbidden:
        assert step not in shopify.calls, step
    async with SessionLocal() as session:
        stored = await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == rec.id))
        assert stored.buildStatus == "pending_review" and not stored.shopifyVariantId
        assert bool(stored.shopifyProductId) is product_known  # a known remote object is recorded for reconciliation


@pytest.mark.parametrize("variants, ok", [
    ([("136.00",)], True),
    ([("136.00",), ("0.00",)], False),       # a second variant left at Shopify's default price
    ([("136.00",), ("135.99",)], False),
    ([("136.00",), (None,)], False),
    ([("0.00",)], False),
    ([], False),
], ids=["single_priced", "extra_unpriced_variant", "extra_mispriced_variant", "variant_without_price", "default_price_only", "no_variants"])
async def test_no_variant_may_become_purchasable_without_the_computed_price(catalog, odoo, monkeypatch, variants, ok):
    rec = await _two_component_build(catalog, odoo)
    edges = [{"node": {"id": f"gid://shopify/ProductVariant/{i}", "price": v[0], "selectedOptions": [], "inventoryItem": {"id": "i", "tracked": False}}} for i, v in enumerate(variants, 1)]
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)

    async def _readback(session, shop, pid):
        shopify.calls.append("read:pricing")
        return {"id": pid, "variants": {"edges": edges}}

    monkeypatch.setattr(builds, "get_product_for_pricing", _readback)
    async with SessionLocal() as session:
        if ok:
            await builds.create_shopify_build_product(session, SHOP, recommendation=rec, custom_name="My Blend", ratios=RATIOS, customer_name=None, customer_email=None)
            assert shopify.calls.index("read:pricing") < shopify.calls.index("activate_product") < shopify.calls.index("publish_to_all_channels")
        else:
            with pytest.raises(builds.BuildWriteAmbiguous):
                await builds.create_shopify_build_product(session, SHOP, recommendation=rec, custom_name="My Blend", ratios=RATIOS, customer_name=None, customer_email=None)
            assert "activate_product" not in shopify.calls and "publish_to_all_channels" not in shopify.calls


async def test_definitive_rejection_is_a_preflight_style_failure_not_pending_review(catalog, odoo, monkeypatch):
    rec = await _two_component_build(catalog, odoo)
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    request = httpx.Request("POST", "https://synthetic.invalid/graphql")
    shopify.fail_on["create_product"] = httpx.HTTPStatusError("422", request=request, response=httpx.Response(422, request=request))
    async with SessionLocal() as session:
        with pytest.raises(httpx.HTTPStatusError):
            await execute_build_commerce(session, SHOP, recommendation_id=rec.id, ratios=RATIOS, name="My Blend")
        assert (await session.scalar(select(FragranceRecommendation.buildStatus).where(FragranceRecommendation.id == rec.id))) == "draft"


def test_customer_wording_matches_what_is_actually_known():
    preflight = [COMMERCE_FAILURES[k][2].lower() for k in ("INSUFFICIENT", "UNCONFIRMED", "SERVICE_UNAVAILABLE")]
    assert all("haven't created" in m or "can't be made" in m for m in preflight)  # true: nothing was attempted
    pending = COMMERCE_FAILURES["build_pending_review"][2].lower()
    for claim in ("haven't created", "was not created", "nothing was created", "has been saved", "is saved", "successfully"):
        assert claim not in pending, claim  # a remote object may exist: claim neither outcome
    assert "couldn't confirm" in pending


# ===========================================================================
# 6. Interruption and retry
# ===========================================================================

class _Crash(BaseException):
    """A process death: not an Exception, so no handler in the application tidies up after it."""


async def _stored(rec_id):
    async with SessionLocal() as session:
        return await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == rec_id))


@pytest.mark.parametrize("crash_at", ["before_external_call", "after_shopify_accepted_before_id_persisted", "after_id_persisted_before_completion"])
async def test_retry_after_a_crash_never_creates_a_second_product(catalog, odoo, monkeypatch, crash_at):
    rec = await _two_component_build(catalog, odoo)
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    accepted = []

    if crash_at == "before_external_call":
        async def _die(*a, **kw):
            raise _Crash()
        monkeypatch.setattr(builds, "require_commerce_inventory", _die)  # dies after `creating` was committed, before any Shopify call
    elif crash_at == "after_shopify_accepted_before_id_persisted":
        async def _accept_then_die(*a, **kw):
            shopify.calls.append("create_product")
            accepted.append(shopify.product_id)  # Shopify has the product ...
            raise _Crash()                         # ... and the process dies before learning its id
        monkeypatch.setattr(builds, "create_product", _accept_then_die)
    else:
        async def _die_at_price(*a, **kw):
            shopify.calls.append("set_variant_price")
            raise _Crash()
        monkeypatch.setattr(builds, "set_variant_price", _die_at_price)

    async with SessionLocal() as session:
        with pytest.raises(_Crash):
            await execute_build_commerce(session, SHOP, recommendation_id=rec.id, ratios=RATIOS, name="My Blend", save_draft=True)

    stored = await _stored(rec.id)
    assert stored.buildStatus == "creating"  # durable, committed BEFORE the creation request could be sent
    assert bool(stored.shopifyProductId) is (crash_at == "after_id_persisted_before_completion")

    # The lock died with the "process". A fresh, healthy retry arrives.
    healthy = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    monkeypatch.setattr(builds, "require_commerce_inventory", commerce_inventory.require_commerce_inventory)
    token = await _token_for(rec)
    async with SessionLocal() as session:
        with pytest.raises(BuildPendingReview):
            await execute_build_commerce(session, SHOP, recommendation_id=rec.id, ratios=RATIOS, name="My Blend", save_draft=True)
    with TestClient(app) as client:
        assert _preview_post(client, rec, token, intent="save_build").json()["code"] == "build_pending_review"
        assert _preview_post(client, rec, token, intent="recreate").json()["code"] == "build_pending_review"
    assert healthy.calls == []  # no read, no write, certainly no second creation
    assert (await _stored(rec.id)).buildStatus == "creating"


def test_recovery_procedure_starts_with_read_only_reconciliation():
    text = open("docs/INVENTORY_COMMERCE_SECURITY.md").read().lower()
    section = text[text.index("operator recovery"):]
    assert section.index("read-only reconciliation") < section.index("buildstatus = '")  # look first, change state afterwards
    assert "never reset the status first" in section


# ===========================================================================
# 7. Network isolation and explicit configuration
# ===========================================================================

async def test_missing_integration_configuration_makes_zero_requests(monkeypatch, network_guard):
    sent = []

    async def _would_send(url):
        sent.append(url)
        return {"ok": True, "json": {}}

    monkeypatch.setattr(odoo_client, "_get_json", _would_send)  # the REAL public functions run; only the transport is a spy
    before = network_guard.real_calls
    for url in ("", "   ", "http://not-https.synthetic.invalid/x", "ftp://x.synthetic.invalid", "inventory.synthetic.invalid/no-scheme"):
        monkeypatch.setattr(settings, "odoo_inventory_url", url)
        monkeypatch.setattr(settings, "odoo_ping_url", url)
        assert (await odoo_client.get_inventory_by_skus(["X"])) == {"ok": False, "status": None, "durationMs": 0, "error": odoo_client.NOT_CONFIGURED, "configured": False}
        assert (await odoo_client.ping_odoo())["configured"] is False
        assert odoo_client.inventory_integration_configured() is False
    assert sent == [] and network_guard.real_calls == before
    monkeypatch.setattr(settings, "odoo_inventory_url", "https://inventory.synthetic.invalid/api/get-inventory")
    await odoo_client.get_inventory_by_skus(["A", "B"])
    assert sent == ["https://inventory.synthetic.invalid/api/get-inventory?skus=A%2CB"]  # explicit configuration is what enables it


async def test_discovery_stays_lenient_and_silent_when_the_integration_is_not_configured(catalog, monkeypatch, network_guard):
    await catalog.add_product(1)
    await catalog.add_product(2)
    monkeypatch.setattr(settings, "odoo_inventory_url", "")
    odoo_inventory.clear_odoo_inventory_cache_for_testing()
    before = len(network_guard.attempts)
    async with SessionLocal() as session:  # the REAL client, nothing mocked
        lenient = await odoo_inventory.evaluate_candidate_inventory(session, {"recommendationId": "r", "recommendedRatio": [{"productTitle": catalog.title(1), "ratioPercent": 60}, {"productTitle": catalog.title(2), "ratioPercent": 40}]})
    assert lenient["buildable"] is True and lenient["inventoryValidated"] is False
    assert len(network_guard.attempts) == before  # and not a single connection was even attempted


@pytest.mark.expects_network_block
async def test_unmocked_odoo_paths_are_blocked_before_any_packet_by_either_import_path(catalog, monkeypatch, network_guard):
    """The Phase 5 incident, reproduced safely: a configured URL and an UNMOCKED client. Both the
    commerce path (module reference) and the discovery path (imported-by-name reference) are
    stopped at the socket layer, before DNS."""
    await catalog.add_product(1)
    await catalog.add_product(2)
    rec = await catalog.add_recommendation([(1, 60), (2, 40)])
    monkeypatch.setattr(settings, "odoo_inventory_url", "https://inventory.synthetic.invalid/api/get-inventory")
    odoo_inventory.clear_odoo_inventory_cache_for_testing()
    attempts_before = len(network_guard.attempts)
    async with SessionLocal() as session:
        decision = (await verify_build_inventory(session, recommendation=rec, ratios=RATIOS))[1]
        lenient = await odoo_inventory.get_oil_inventory_for_product_titles(session, [catalog.title(1), catalog.title(2)])
    # The application swallowed the error into "unavailable" -- exactly why the guard also fails
    # the test at teardown unless it is marked expects_network_block.
    assert decision.state is InventoryState.SERVICE_UNAVAILABLE and all(r["mappingStatus"] == "LOOKUP_FAILED" for r in lenient["results"].values())
    blocked = network_guard.attempts[attempts_before:]
    assert len(blocked) == 2 and all("inventory.synthetic.invalid" in b for b in blocked)
    # The only real socket activity was the disposable database.
    assert all("synthetic" not in a for a in blocked if a.startswith("connect"))


@pytest.mark.expects_network_block
def test_raw_sockets_http_clients_dns_and_udp_are_all_blocked_without_a_real_call(network_guard):
    real_before = network_guard.real_calls
    with pytest.raises(ExternalNetworkBlocked):
        socket.create_connection(("192.0.2.10", 443), timeout=1)  # TEST-NET-1, never routable
    with pytest.raises(ExternalNetworkBlocked):
        socket.getaddrinfo("example.invalid", 443)
    with pytest.raises(ExternalNetworkBlocked):
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("192.0.2.10", 53))
    with pytest.raises(ExternalNetworkBlocked):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect_ex(("192.0.2.10", 80))
    with pytest.raises((httpx.ConnectError, ExternalNetworkBlocked)):
        httpx.get("https://example.invalid/x", timeout=1)
    assert network_guard.real_calls == real_before  # nothing was let through to the real socket layer


@pytest.mark.expects_network_block
def test_localhost_is_not_a_wildcard(network_guard):
    for address in (("127.0.0.1", 5435), ("127.0.0.1", 80), ("localhost", 5432), ("::1", 5432)):
        assert network_guard.allowed(socket.AF_INET, address) is False or address in network_guard.tcp
    with pytest.raises(ExternalNetworkBlocked):
        socket.create_connection(("127.0.0.1", 5435), timeout=1)  # an unrelated local service
    if hasattr(socket, "AF_UNIX"):
        with pytest.raises(ExternalNetworkBlocked):
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).connect("/tmp/some-unrelated.sock")


async def test_the_disposable_database_and_in_process_clients_still_work(network_guard):
    before = network_guard.real_calls
    async with SessionLocal() as session:
        assert await session.scalar(select(1)) == 1
    assert network_guard.real_calls > before  # the one allowed destination
    with TestClient(app) as client:
        assert client.get("/healthz").status_code in (200, 404)  # in-process: no socket needed


def test_deterministic_tests_cannot_enable_live_network(network_guard, request):
    assert network_guard.enabled is True
    assert request.node.get_closest_marker("live_ai") is None
    assert os.environ.get("ALLOW_LIVE_NETWORK") != "1"
    source = open("tests/conftest.py").read()
    assert 'get_closest_marker("live_ai") is not None and _os.environ.get("ALLOW_LIVE_NETWORK") == "1"' in source


def test_no_real_external_hostname_is_a_configuration_default():
    for name, field_info in Settings.model_fields.items():
        default = field_info.default
        if isinstance(default, str) and "://" in default:
            host = default.split("://", 1)[1].split("/", 1)[0]
            assert host.endswith((".example.com", ".invalid", "localhost")) or host in ("api.openai.com",), (name, default)
    assert "odoo.com" not in json.dumps({k: str(v.default) for k, v in Settings.model_fields.items()})
