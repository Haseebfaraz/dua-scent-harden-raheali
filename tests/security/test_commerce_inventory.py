"""Phase 5 (F9) regressions: inventory uncertainty must never authorize a controlled commerce
write, and no Shopify write may happen before every required check has passed.

Everything here is synthetic: fake titles, fake Odoo item codes, a mocked Odoo client, mocked
Shopify writes, a disposable local PostgreSQL. Nothing in this file contacts Odoo or Shopify.

WHAT THESE TESTS DO NOT PROVE: a mocked lookup is not a reservation. Nothing here shows that stock
is held, that another channel cannot consume it, or that a Shopify variant created earlier cannot
be bought later. See docs/INVENTORY_COMMERCE_SECURITY.md sections 7 and 9.
"""

import asyncio
import hashlib
import hmac as _hmac
import json
import logging
import math
import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.ai import conversation_flow, safe_views
from app.ai.safe_views import AVAILABILITY_GUIDANCE, CustomerSafeRecommendation, recommendation_presentation_messages
from app.api import preview as preview_module
from app.config import settings
from app.db.ids import new_id
from app.db.models import (
    BuildCapability, Conversation, CustomerProfileState, FragranceProduct, FragranceRecommendation, OdooOilMapping,
    RecommendationInventorySnapshot,
)
from app.db.session import SessionLocal
from app.db.time import utcnow
from app.fragrance.formulas import MAX_OIL_ML
from app.integrations import odoo_client
from app.main import app
from app.services import build_commerce, commerce_inventory, odoo_inventory
from app.services.build_capability import issue_build_token
from app.services.build_commerce import BuildOperationInProgress, BuildPendingReview, build_commerce_lock, execute_build_commerce
from app.services.commerce_inventory import (
    COMMERCE_FAILURES, InventoryNotVerified, InventoryState, InventoryDecision, RequirementsUnknown,
    compute_build_requirements, require_commerce_inventory, verify_build_inventory,
)
from app.services.conversation_capability import create_conversation_with_capability
from app.services.inventory_snapshot import save_inventory_snapshot
from app.shopify import builds
from app.shopify.build_input import InvalidRatios

SECRET = "commerce-inventory-test-secret"
SHOP = "test-shop.myshopify.com"
RATIOS = {"top": 34, "middle": 33, "base": 33}
REQUIRED = float(MAX_OIL_ML)  # per distinct Odoo item, per bottle
PREFIX = "pytest-f9"
LOCATION = "SYNTH/Stock"
PRIVATE = ("FAKE-OIL-", "SYNTH/Stock", "available_qty", "on_hand_qty", "default_code", "odooSku", "onHandQty", "requiredOilMl", "limitingSku")


def _sign(params: dict) -> str:
    message = "".join(f"{k}={v}" for k, v in sorted(params.items()))
    return _hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()


def _proxy(**extra) -> dict:
    params = {"shop": SHOP, "timestamp": "1", "logged_in_customer_id": "", **extra}
    return {**params, "signature": _sign(params)}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(settings, "shopify_api_secret", SECRET)
    monkeypatch.setattr(settings, "shopify_shop_domain", SHOP)
    monkeypatch.setattr(settings, "allowed_origins", "")

    async def _fake_token(*_a, **_kw):
        return "fake-admin-token-for-tests", "client_credentials"

    monkeypatch.setattr(preview_module, "get_admin_access_token", _fake_token)
    odoo_inventory.clear_odoo_inventory_cache_for_testing()
    # Phase 5A: a complete SYNTHETIC source contract. Every fact the policy needs is declared here
    # and echoed by the mocked source below; test_commerce_policy_5a.py removes them one by one.
    monkeypatch.setattr(settings, "odoo_inventory_url", "https://odoo.synthetic.invalid/api/get-inventory")
    monkeypatch.setattr(settings, "odoo_inventory_location_scope", LOCATION)
    monkeypatch.setattr(settings, "odoo_inventory_quantity_semantics", "UNRESERVED_AVAILABLE")
    monkeypatch.setattr(settings, "manufacturing_max_oil_ml_per_bottle", float(MAX_OIL_ML))


class Catalog:
    """Synthetic catalog + Odoo mappings + one recommendation, all cleaned up afterwards."""

    def __init__(self):
        self.tag = uuid.uuid4().hex[:8]
        self.product_ids: list[str] = []
        self.recommendation_ids: list[str] = []
        self.conversation_ids: list[str] = []

    def title(self, n: int) -> str:
        return f"{PREFIX} Blend {self.tag} {n}"

    def sku(self, n: int) -> str:
        return f"FAKE-OIL-{self.tag}-{n}"

    async def add_product(self, n: int, *, sku: str | None = "auto", unit: str | None = "ml", active: bool = True, mapped: bool = True) -> None:
        from app.fragrance.normalization import normalize_product_name

        async with SessionLocal() as session:
            product_id = new_id()
            session.add(FragranceProduct(id=product_id, title=self.title(n), normalizedTitle=normalize_product_name(self.title(n)), notesJson=["Rose", "Amber", "Musk"], pricePer5ml=20.0, createdAt=utcnow(), updatedAt=utcnow()))
            if mapped:
                session.add(OdooOilMapping(id=new_id(), fragranceProductId=product_id, odooSku=self.sku(n) if sku == "auto" else sku, unitOfMeasure=unit, active=active, createdAt=utcnow(), updatedAt=utcnow()))
            await session.commit()
            self.product_ids.append(product_id)

    async def add_recommendation(self, components: list[tuple[int, float]], **overrides) -> FragranceRecommendation:
        conversation_id = f"{PREFIX}-conv-{uuid.uuid4().hex[:8]}"
        defaults = dict(
            id=f"{PREFIX}-rec-{uuid.uuid4().hex[:8]}", conversationId=conversation_id, customerProfileJson={"likes": ["Rose"]},
            productsJson=[{"title": self.title(n), "notes": ["Rose", "Amber", "Musk"], "contribution": "x"} for n, _ in components],
            combinationType="HYBRID", scoreJson={}, evidenceJson={}, ratiosJson=[{"productTitle": self.title(n), "ratioPercent": pct} for n, pct in components],
            customerFacingJson={"customerFacingName": "Test Blend"}, status="confirmed", createdAt=utcnow(),
        )
        defaults.update(overrides)
        async with SessionLocal() as session:
            session.add(Conversation(id=conversation_id, createdAt=utcnow(), updatedAt=utcnow()))
            record = FragranceRecommendation(**defaults)
            session.add(record)
            await session.commit()
        self.recommendation_ids.append(defaults["id"])
        self.conversation_ids.append(conversation_id)
        return record

    async def cleanup(self) -> None:
        async with SessionLocal() as session:
            for rid in self.recommendation_ids:
                await session.execute(delete(RecommendationInventorySnapshot).where(RecommendationInventorySnapshot.recommendationId == rid))
                await session.execute(delete(BuildCapability).where(BuildCapability.recommendationId == rid))
                await session.execute(delete(FragranceRecommendation).where(FragranceRecommendation.id == rid))
            for cid in self.conversation_ids:
                await session.execute(delete(CustomerProfileState).where(CustomerProfileState.conversationId == cid))
                await session.execute(delete(Conversation).where(Conversation.id == cid))
            for pid in self.product_ids:
                await session.execute(delete(OdooOilMapping).where(OdooOilMapping.fragranceProductId == pid))
                await session.execute(delete(FragranceProduct).where(FragranceProduct.id == pid))
            await session.commit()


@pytest.fixture
async def catalog():
    c = Catalog()
    yield c
    await c.cleanup()


class Odoo:
    """Mocked Odoo client. `stock` maps item code -> on_hand_qty; `response` overrides everything."""

    def __init__(self, monkeypatch):
        self.calls: list[list[str]] = []
        self.stock: dict[str, object] = {}
        self.response = None
        self.raises = None

        async def _lookup(skus):
            self.calls.append(list(skus))
            if self.raises:
                raise self.raises
            if self.response is not None:
                return self.response(skus) if callable(self.response) else self.response
            return {"ok": True, "status": 200, "json": {"success": True, "location": LOCATION, "products": [{"name": "x", "default_code": s, "uom": "ml", "available_qty": self.stock[s], "on_hand_qty": self.stock[s]} for s in skus if s in self.stock]}}

        monkeypatch.setattr(odoo_client, "get_inventory_by_skus", _lookup)
        # The discovery path imported the function by name: patch that reference too, so no test in
        # this file can ever reach the real Odoo endpoint.
        monkeypatch.setattr(odoo_inventory, "get_inventory_by_skus", _lookup)


@pytest.fixture
def odoo(monkeypatch):
    return Odoo(monkeypatch)


class ShopifyWrites:
    """Spies on EVERY Shopify build write plus the pricing read."""

    WRITES = ("create_product", "attach_product_media", "publish_to_all_channels", "set_variant_price", "activate_product", "create_variant", "rename_product", "set_inventory_item_untracked")

    def __init__(self, monkeypatch, *, existing_variants=None, recommendation_id=None, product_id="gid://shopify/Product/555"):
        self.calls: list[str] = []
        self.product_id = product_id
        self.fail_on: dict[str, Exception] = {}

        def _spy(name, result=None):
            async def _f(*a, **kw):
                self.calls.append(name)
                if name in self.fail_on:
                    raise self.fail_on[name]
                return result
            return _f

        monkeypatch.setattr(builds, "create_product", _spy("create_product", {"id": product_id, "handle": "custom-blend"}))
        monkeypatch.setattr(builds, "attach_product_media", _spy("attach_product_media"))
        monkeypatch.setattr(builds, "publish_to_all_channels", _spy("publish_to_all_channels"))
        monkeypatch.setattr(builds, "set_variant_price", _spy("set_variant_price"))
        monkeypatch.setattr(builds, "activate_product", _spy("activate_product"))
        monkeypatch.setattr(builds, "create_variant", _spy("create_variant", {"id": "gid://shopify/ProductVariant/2", "price": "60.00"}))
        monkeypatch.setattr(builds, "rename_product", _spy("rename_product"))
        monkeypatch.setattr(builds, "set_inventory_item_untracked", _spy("set_inventory_item_untracked"))
        monkeypatch.setattr(builds, "get_default_variant_id", _spy("read:default_variant", "gid://shopify/ProductVariant/1"))

        async def _pricing(session, shop, pid):
            self.calls.append("read:pricing")
            return {
                "id": product_id, "title": "Old Title", "vendor": "The Dua Brand", "templateSuffix": "custom-scent",
                "metafield": {"value": json.dumps({"recommendationId": recommendation_id, "layers": [
                    {"position": "top", "quantityMl": 17.0, "pricePer5ml": 20}, {"position": "middle", "quantityMl": 8.5, "pricePer5ml": 20}, {"position": "base", "quantityMl": 8.5, "pricePer5ml": 20}]})},
                "variants": {"edges": existing_variants or [{"node": {"id": "gid://shopify/ProductVariant/1", "price": "136.00", "inventoryItem": {"id": "gid://shopify/InventoryItem/1", "tracked": True},
                                                                       "selectedOptions": [{"name": "Top Note", "value": "Rose (50%)"}, {"name": "Middle Note", "value": "Amber (25%)"}, {"name": "Base Note", "value": "Musk (25%)"}]}}]},
            }

        monkeypatch.setattr(builds, "get_product_for_pricing", _pricing)

        async def _handle(*a, **kw):
            return "custom-blend"

        monkeypatch.setattr(build_commerce, "get_product_handle", _handle)

    @property
    def writes(self) -> list[str]:
        return [c for c in self.calls if c in self.WRITES]


async def _two_component_build(catalog, odoo, *, stock=(100.0, 100.0), **rec_overrides):
    await catalog.add_product(1)
    await catalog.add_product(2)
    odoo.stock = {catalog.sku(1): stock[0], catalog.sku(2): stock[1]}
    return await catalog.add_recommendation([(1, 60), (2, 40)], **rec_overrides)


async def _verify(recommendation, ratios=RATIOS, quantity=1) -> InventoryDecision:
    async with SessionLocal() as session:
        return (await verify_build_inventory(session, recommendation=recommendation, ratios=ratios, quantity=quantity))[1]


# ===========================================================================
# 1. Inventory states
# ===========================================================================

async def test_every_component_available_is_verified_available(catalog, odoo):
    rec = await _two_component_build(catalog, odoo)
    result = await _verify(rec)
    assert result.state is InventoryState.POLICY_SATISFIED and result.reasons == () and result.observation.item_count == 2
    assert odoo.calls == [sorted([catalog.sku(1), catalog.sku(2)])]  # ONE batched request for every item


async def test_exact_stock_boundary(catalog, odoo):
    rec = await _two_component_build(catalog, odoo, stock=(REQUIRED, REQUIRED))
    assert (await _verify(rec)).state is InventoryState.POLICY_SATISFIED
    odoo.stock[catalog.sku(2)] = REQUIRED - 0.01
    assert (await _verify(rec)).state is InventoryState.INSUFFICIENT


async def test_one_insufficient_component_is_never_hidden_by_an_available_one(catalog, odoo):
    rec = await _two_component_build(catalog, odoo, stock=(5000.0, 1.0))
    result = await _verify(rec)
    assert result.state is InventoryState.INSUFFICIENT and result.reasons == (commerce_inventory.INSUFFICIENT,)


async def test_all_components_insufficient(catalog, odoo):
    rec = await _two_component_build(catalog, odoo, stock=(0, 0.5))
    assert (await _verify(rec)).state is InventoryState.INSUFFICIENT


@pytest.mark.parametrize("setup, reason", [
    (dict(mapped=False), commerce_inventory.MAPPING_MISSING),
    (dict(active=False), commerce_inventory.MAPPING_INACTIVE),
    (dict(sku="  "), commerce_inventory.MAPPING_MISSING),
    (dict(unit=None), commerce_inventory.UNIT_UNCONFIRMED),
    (dict(unit="L"), commerce_inventory.UNIT_UNCONFIRMED),
    (dict(unit="units"), commerce_inventory.UNIT_UNCONFIRMED),
    (dict(unit="oz"), commerce_inventory.UNIT_UNCONFIRMED),
], ids=["missing_mapping", "inactive_mapping", "blank_item_code", "unit_not_recorded", "unit_litres", "unit_units", "unit_ounces"])
async def test_mapping_and_unit_problems_are_unknown_and_never_query_odoo(catalog, odoo, setup, reason):
    await catalog.add_product(1)
    await catalog.add_product(2, **setup)
    odoo.stock = {catalog.sku(1): 5000.0, catalog.sku(2): 5000.0}
    rec = await catalog.add_recommendation([(1, 60), (2, 40)])
    result = await _verify(rec)
    assert result.state is InventoryState.UNCONFIRMED and reason in result.reasons
    assert odoo.calls == []  # no partial lookup that could be misread as partial approval


async def test_component_missing_from_the_catalog_is_unknown(catalog, odoo):
    await catalog.add_product(1)
    odoo.stock = {catalog.sku(1): 5000.0}
    rec = await catalog.add_recommendation([(1, 60), (2, 40)])  # product 2 has no catalog row at all
    assert (await _verify(rec)).state is InventoryState.UNCONFIRMED


async def test_missing_response_entry_and_truncated_batch_are_unknown(catalog, odoo):
    rec = await _two_component_build(catalog, odoo)
    del odoo.stock[catalog.sku(2)]  # Odoo answers, but only for one of the two items asked about
    result = await _verify(rec)
    assert result.state is InventoryState.UNCONFIRMED and result.reasons == (commerce_inventory.RESPONSE_INCOMPLETE,)


async def test_duplicate_rows_for_one_item_are_ambiguous(catalog, odoo):
    rec = await _two_component_build(catalog, odoo)
    odoo.response = lambda skus: {"ok": True, "json": {"success": True, "location": LOCATION, "products": [{"default_code": s, "available_qty": 5000} for s in skus] + [{"default_code": skus[0], "available_qty": 0}]}}
    result = await _verify(rec)
    assert result.state is InventoryState.UNCONFIRMED and commerce_inventory.RESPONSE_AMBIGUOUS in result.reasons


@pytest.mark.parametrize("response", [
    {"ok": True, "json": None},
    {"ok": True, "json": "<html>502</html>"},
    {"ok": True, "json": {"success": False, "products": []}},
    {"ok": True, "json": {"success": True}},
    {"ok": True, "json": {"success": True, "products": {"not": "a list"}}},
    {"ok": True, "json": {"success": "true", "products": []}},
    {"ok": True, "json": []},
], ids=["no_json", "html", "success_false", "no_products", "products_not_list", "success_not_boolean", "json_array"])
async def test_malformed_responses_are_service_unavailable(catalog, odoo, response):
    rec = await _two_component_build(catalog, odoo)
    odoo.response = response
    assert (await _verify(rec)).state is InventoryState.SERVICE_UNAVAILABLE


@pytest.mark.parametrize("response", [
    {"ok": False, "status": None, "error": "timeout"},
    {"ok": False, "status": 401},
    {"ok": False, "status": 500},
    None,
    "garbage",
], ids=["timeout", "auth_failure", "server_error", "none", "not_a_dict"])
async def test_service_failures_are_service_unavailable(catalog, odoo, response):
    rec = await _two_component_build(catalog, odoo)
    odoo.response = (lambda skus: response)
    assert (await _verify(rec)).state is InventoryState.SERVICE_UNAVAILABLE


async def test_client_exception_is_service_unavailable_not_a_crash(catalog, odoo):
    rec = await _two_component_build(catalog, odoo)
    odoo.raises = RuntimeError("connection reset")
    assert (await _verify(rec)).state is InventoryState.SERVICE_UNAVAILABLE


@pytest.mark.parametrize("bad", [-1, -0.01, float("nan"), float("inf"), float("-inf"), "100", None, True, [100], {"qty": 100}],
                         ids=["negative", "small_negative", "nan", "inf", "neg_inf", "string", "null", "boolean", "list", "object"])
async def test_invalid_quantities_are_unknown_never_coerced(catalog, odoo, bad):
    rec = await _two_component_build(catalog, odoo)
    odoo.stock[catalog.sku(2)] = bad
    result = await _verify(rec)
    assert result.state is InventoryState.UNCONFIRMED and commerce_inventory.QUANTITY_INVALID in result.reasons


async def test_rows_without_an_item_code_make_the_answer_unusable(catalog, odoo):
    rec = await _two_component_build(catalog, odoo)
    odoo.response = lambda skus: {"ok": True, "json": {"success": True, "location": LOCATION, "products": [{"available_qty": 5000}, "junk", *[{"default_code": s, "available_qty": 5000} for s in skus]]}}
    assert (await _verify(rec)).state is InventoryState.UNCONFIRMED


# ===========================================================================
# 2. Requirements
# ===========================================================================

def _rec(**overrides):
    base = dict(id="rec-unit", productsJson=[{"title": "Alpha"}, {"title": "Beta"}], ratiosJson=[{"productTitle": "Alpha", "ratioPercent": 60}, {"productTitle": "Beta", "ratioPercent": 40}])
    base.update(overrides)
    return SimpleNamespace(**base)


def test_requirement_is_the_documented_worst_case_bound_with_deterministic_rounding():
    req = compute_build_requirements(_rec(), RATIOS)
    assert req.required_ml_per_item == Decimal("14.00") == Decimal(MAX_OIL_ML).quantize(Decimal("0.01"))
    assert req.quantity == 1 and req.component_titles == ("alpha", "beta")
    assert dict(req.default_formula_ml) == {"alpha": 7.8, "beta": 5.2}  # the recommendation's own 13 ml formula, informational
    assert compute_build_requirements(_rec(), RATIOS).fingerprint == req.fingerprint  # deterministic


@pytest.mark.parametrize("ratios", [{"top": 34, "middle": 33, "base": 33}, {"top": 50, "middle": 25, "base": 25}, {"top": 1, "middle": 1, "base": 98}, {"top": 98, "middle": 1, "base": 1}])
def test_existing_valid_ratio_examples_are_accepted_and_bound_holds_for_every_ratio(ratios):
    req = compute_build_requirements(_rec(), ratios)
    # Whatever the slider says, no single oil can exceed the bottle's total oil.
    assert req.required_ml_per_item >= Decimal(str(max(dict(req.default_formula_ml).values())))


@pytest.mark.parametrize("ratios", [{"top": 0, "middle": 50, "base": 50}, {"top": 100, "middle": 0, "base": 0}, {"top": 34, "middle": 33, "base": 34}, {"top": -1, "middle": 51, "base": 50}, {"top": 33.5, "middle": 33.5, "base": 33}, None, {}])
def test_phase_one_ratio_rules_are_unchanged(ratios):
    with pytest.raises(InvalidRatios):
        compute_build_requirements(_rec(), ratios)


def test_different_final_ratios_or_quantity_or_recipe_change_the_fingerprint():
    base = compute_build_requirements(_rec(), RATIOS).fingerprint
    assert compute_build_requirements(_rec(), {"top": 50, "middle": 25, "base": 25}).fingerprint != base
    assert compute_build_requirements(_rec(id="other"), RATIOS).fingerprint != base
    changed = _rec(productsJson=[{"title": "Alpha"}, {"title": "Gamma"}], ratiosJson=[{"productTitle": "Alpha", "ratioPercent": 60}, {"productTitle": "Gamma", "ratioPercent": 40}])
    assert compute_build_requirements(changed, RATIOS).fingerprint != base
    reweighted = _rec(ratiosJson=[{"productTitle": "Alpha", "ratioPercent": 50}, {"productTitle": "Beta", "ratioPercent": 50}])
    assert compute_build_requirements(reweighted, RATIOS).fingerprint != base


def test_only_the_single_bottle_the_product_actually_sells_is_supported():
    assert compute_build_requirements(_rec(), RATIOS, quantity=1).required_ml_per_item == Decimal("14.00")
    for bad in (0, -1, 2, 3, 10, 1.0, 1.5, "1", True, None):
        with pytest.raises(RequirementsUnknown):
            compute_build_requirements(_rec(), RATIOS, quantity=bad)


@pytest.mark.parametrize("overrides", [
    dict(productsJson=None), dict(productsJson=[]), dict(productsJson=[{"title": ""}]), dict(productsJson=[{"notes": []}]),
    dict(productsJson=[{"title": "Alpha"}, {"title": "alpha"}]),
    dict(productsJson=[{"title": str(i)} for i in range(5)]),
    dict(ratiosJson=None), dict(ratiosJson=[]), dict(ratiosJson=[{"productTitle": "Alpha", "ratioPercent": 100}]),
    dict(ratiosJson=[{"productTitle": "Alpha", "ratioPercent": 60}, {"productTitle": "Other", "ratioPercent": 40}]),
    dict(ratiosJson=[{"productTitle": "Alpha", "ratioPercent": 60}, {"productTitle": "Beta", "ratioPercent": 10}]),
    dict(ratiosJson=[{"productTitle": "Alpha", "ratioPercent": float("nan")}, {"productTitle": "Beta", "ratioPercent": 40}]),
    dict(ratiosJson=[{"productTitle": "Alpha", "ratioPercent": float("inf")}, {"productTitle": "Beta", "ratioPercent": 40}]),
    dict(ratiosJson=[{"productTitle": "Alpha", "ratioPercent": -60}, {"productTitle": "Beta", "ratioPercent": 160}]),
    dict(ratiosJson=[{"productTitle": "Alpha", "ratioPercent": "60"}, {"productTitle": "Beta", "ratioPercent": 40}]),
    dict(ratiosJson=[{"productTitle": "Alpha", "ratioPercent": True}, {"productTitle": "Beta", "ratioPercent": 40}]),
])
def test_missing_or_incoherent_requirement_information_is_unknown(overrides):
    with pytest.raises(RequirementsUnknown):
        compute_build_requirements(_rec(**overrides), RATIOS)


async def test_requirements_unknown_blocks_without_any_lookup(catalog, odoo):
    rec = await _two_component_build(catalog, odoo, ratiosJson=[])
    result = await _verify(rec)
    assert result.state is InventoryState.UNCONFIRMED and result.reasons == (commerce_inventory.REQUIREMENTS_UNKNOWN,) and odoo.calls == []


async def test_components_sharing_one_odoo_item_are_aggregated_into_one_demand(catalog, odoo):
    shared = f"FAKE-OIL-{catalog.tag}-shared"
    await catalog.add_product(1, sku=shared)
    await catalog.add_product(2, sku=shared)
    rec = await catalog.add_recommendation([(1, 60), (2, 40)])
    odoo.stock = {shared: REQUIRED}
    result = await _verify(rec)
    assert odoo.calls == [[shared]] and result.observation.item_count == 1  # asked once, checked once against the whole bottle's oil
    assert result.state is InventoryState.POLICY_SATISFIED
    odoo.stock[shared] = REQUIRED - 0.5
    assert (await _verify(rec)).state is InventoryState.INSUFFICIENT


# ===========================================================================
# 3. Freshness: nothing earlier is ever reused
# ===========================================================================

async def test_stale_mismatched_or_forged_evidence_never_authorizes(catalog, odoo):
    rec = await _two_component_build(catalog, odoo)
    async with SessionLocal() as session:
        fingerprint, ok = await verify_build_inventory(session, recommendation=rec, ratios=RATIOS)
        other_fingerprint, _ = await verify_build_inventory(session, recommendation=rec, ratios={"top": 50, "middle": 25, "base": 25})
    assert ok.authorizes(fingerprint) == (True, None)
    later = utcnow() + timedelta(seconds=settings.commerce_inventory_max_age_seconds + 1)
    assert ok.authorizes(fingerprint, now=later) == (False, commerce_inventory.STALE)
    assert ok.authorizes(fingerprint, now=utcnow() - timedelta(seconds=5)) == (False, commerce_inventory.STALE)
    assert other_fingerprint != fingerprint and ok.authorizes(other_fingerprint) == (False, commerce_inventory.FINGERPRINT_MISMATCH)
    assert ok.authorizes("") == (False, commerce_inventory.FINGERPRINT_MISMATCH)
    # A caller cannot construct its own approval: only decisions sealed inside the gate count.
    forged = InventoryDecision(state=InventoryState.POLICY_SATISFIED, reasons=(), observation=ok.observation, operation_fingerprint=fingerprint, checked_at=utcnow())
    assert forged.authorizes(fingerprint) == (False, commerce_inventory.NOT_ISSUED_BY_GATE)


async def test_failed_refresh_after_an_earlier_success_blocks_and_every_action_looks_up_again(catalog, odoo):
    rec = await _two_component_build(catalog, odoo)
    async with SessionLocal() as session:
        await require_commerce_inventory(session, recommendation=rec, ratios=RATIOS)
        await require_commerce_inventory(session, recommendation=rec, ratios=RATIOS)
        assert len(odoo.calls) == 2  # no commerce cache: a second action is a second lookup
        odoo.response = {"ok": False, "status": None, "error": "timeout"}
        with pytest.raises(InventoryNotVerified) as err:
            await require_commerce_inventory(session, recommendation=rec, ratios=RATIOS)
        assert err.value.state is InventoryState.SERVICE_UNAVAILABLE


async def test_recommendation_time_evidence_and_the_recommendation_cache_never_authorize_commerce(catalog, odoo):
    rec = await _two_component_build(catalog, odoo)
    async with SessionLocal() as session:
        # A glowing recommendation-time snapshot ...
        await save_inventory_snapshot(session, recommendation_id=rec.id, inventory_validated=True, buildable=True, checked_at=utcnow(), oil_total_ml=13, alcohol_ml=21,
                                      request_status="ok", max_buildable_bottles=500, limiting_sku=None, components=[])
        # ... and a warm recommendation cache saying plenty is on hand ...
        warm = await odoo_inventory.get_oil_inventory_for_product_titles(session, [catalog.title(1), catalog.title(2)])
        assert all(r["availableOilMl"] == 100.0 for r in warm["results"].values())
        # ... then the stock really runs out.
        odoo.stock = {catalog.sku(1): 0, catalog.sku(2): 0}
        cached = await odoo_inventory.get_oil_inventory_for_product_titles(session, [catalog.title(1), catalog.title(2)])
        assert all(r["availableOilMl"] == 100.0 for r in cached["results"].values())  # the lenient path still serves its cache
        with pytest.raises(InventoryNotVerified) as err:
            await require_commerce_inventory(session, recommendation=rec, ratios=RATIOS)
        assert err.value.state is InventoryState.INSUFFICIENT


async def test_recommendation_time_fallback_semantics_are_not_commerce_approval(catalog, odoo):
    """The discovery path deliberately returns buildable=True / inventoryValidated=False when it
    cannot tell. That pair must never be read as permission."""
    await catalog.add_product(1, mapped=False)
    await catalog.add_product(2, mapped=False)
    rec = await catalog.add_recommendation([(1, 60), (2, 40)])
    async with SessionLocal() as session:
        lenient = await odoo_inventory.evaluate_candidate_inventory(session, {"recommendationId": rec.id, "recommendedRatio": [{"productTitle": catalog.title(1), "ratioPercent": 60}, {"productTitle": catalog.title(2), "ratioPercent": 40}]})
        assert lenient["buildable"] is True and lenient["inventoryValidated"] is False
        with pytest.raises(InventoryNotVerified) as err:
            await require_commerce_inventory(session, recommendation=rec, ratios=RATIOS)
        assert err.value.state is InventoryState.UNCONFIRMED


def test_there_is_no_setting_that_turns_unknown_into_approval():
    names = [n.lower() for n in type(settings).model_fields]
    assert not [n for n in names if "inventory" in n and any(w in n for w in ("bypass", "skip", "disable", "allow_unknown", "fail_open", "optional"))]


# ===========================================================================
# 4. Mutation ordering: zero Shopify writes on every failure, both write paths
# ===========================================================================

FAILURE_SETUPS = {
    "insufficient": (lambda c, o: o.stock.update({c.sku(2): 1.0}), InventoryState.INSUFFICIENT),
    "missing_entry": (lambda c, o: o.stock.pop(c.sku(2)), InventoryState.UNCONFIRMED),
    "invalid_quantity": (lambda c, o: o.stock.update({c.sku(2): float("nan")}), InventoryState.UNCONFIRMED),
    "timeout": (lambda c, o: setattr(o, "response", {"ok": False, "status": None, "error": "timeout"}), InventoryState.SERVICE_UNAVAILABLE),
    "malformed": (lambda c, o: setattr(o, "response", {"ok": True, "json": {"success": True}}), InventoryState.SERVICE_UNAVAILABLE),
    "exception": (lambda c, o: setattr(o, "raises", RuntimeError("boom")), InventoryState.SERVICE_UNAVAILABLE),
}


@pytest.mark.parametrize("failure", sorted(FAILURE_SETUPS))
async def test_first_time_creation_writes_nothing_when_inventory_is_not_verified(catalog, odoo, monkeypatch, failure):
    rec = await _two_component_build(catalog, odoo)
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    setup, expected = FAILURE_SETUPS[failure]
    setup(catalog, odoo)
    async with SessionLocal() as session:
        with pytest.raises(InventoryNotVerified) as err:
            await builds.create_shopify_build_product(session, SHOP, recommendation=rec, custom_name="My Blend", ratios=RATIOS, customer_name="Sam", customer_email="sam@example.test")
    assert err.value.state is expected and shopify.writes == []


@pytest.mark.parametrize("failure", sorted(FAILURE_SETUPS))
@pytest.mark.parametrize("ratios", [{"top": 50, "middle": 25, "base": 25}, {"top": 10, "middle": 10, "base": 80}], ids=["reuses_existing_variant", "needs_new_variant"])
async def test_existing_build_reprice_writes_nothing_and_returns_no_variant_when_not_verified(catalog, odoo, monkeypatch, failure, ratios):
    rec = await _two_component_build(catalog, odoo, shopifyProductId="gid://shopify/Product/555", buildStatus="saved")
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    setup, expected = FAILURE_SETUPS[failure]
    setup(catalog, odoo)
    async with SessionLocal() as session:
        with pytest.raises(InventoryNotVerified) as err:
            await builds.reprice_existing_build(session, SHOP, recommendation=rec, ratios=ratios, name="A New Name")
    assert err.value.state is expected
    assert shopify.writes == [] and shopify.calls == ["read:pricing"]  # no untrack, no variant, no rename


async def test_verified_build_reaches_the_shopify_writes_in_order_with_publish_last(catalog, odoo, monkeypatch):
    rec = await _two_component_build(catalog, odoo)
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    async with SessionLocal() as session:
        result = await builds.create_shopify_build_product(session, SHOP, recommendation=rec, custom_name="My Blend", ratios=RATIOS, customer_name="Sam", customer_email="sam@example.test")
    assert shopify.writes == ["create_product", "attach_product_media", "set_variant_price", "activate_product", "publish_to_all_channels"]
    assert len(odoo.calls) == 1 and result["variantId"] and math.isclose(result["price"], 136.0)  # price unchanged: 34 ml at $20 / 5 ml


async def test_verified_reprice_still_creates_the_variant_and_renames_last(catalog, odoo, monkeypatch):
    rec = await _two_component_build(catalog, odoo, shopifyProductId="gid://shopify/Product/555", buildStatus="saved")
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    async with SessionLocal() as session:
        result = await builds.reprice_existing_build(session, SHOP, recommendation=rec, ratios={"top": 10, "middle": 10, "base": 80}, name="A New Name")
    assert shopify.writes == ["create_variant", "rename_product"] and result["created"] is True


@pytest.mark.parametrize("bad_input", [dict(ratios={"top": 1, "middle": 1, "base": 1}), dict(custom_name="x" * 500)], ids=["invalid_ratios", "invalid_name"])
async def test_invalid_input_never_reaches_inventory_or_shopify(catalog, odoo, monkeypatch, bad_input):
    rec = await _two_component_build(catalog, odoo)
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    kwargs = dict(recommendation=rec, custom_name="My Blend", ratios=RATIOS, customer_name=None, customer_email=None) | bad_input
    async with SessionLocal() as session:
        with pytest.raises(Exception):
            await builds.create_shopify_build_product(session, SHOP, **kwargs)
    assert odoo.calls == [] and shopify.writes == []


async def test_product_identity_failure_never_reaches_inventory(catalog, odoo, monkeypatch):
    rec = await _two_component_build(catalog, odoo, shopifyProductId="gid://shopify/Product/555", buildStatus="saved")
    shopify = ShopifyWrites(monkeypatch, recommendation_id="someone-elses-recommendation")
    async with SessionLocal() as session:
        with pytest.raises(builds.BuildProductMismatch):
            await builds.reprice_existing_build(session, SHOP, recommendation=rec, ratios=RATIOS, name=None)
    assert odoo.calls == [] and shopify.writes == []


# ===========================================================================
# 5. Routes: authorization first, then the gate, then customer-safe failures
# ===========================================================================

async def _token_for(rec) -> str:
    async with SessionLocal() as session:
        return await issue_build_token(session, recommendation_id=rec.id, conversation_id=rec.conversationId, shop=SHOP)


def _preview_post(client, rec, token, intent="save_build", ratios=RATIOS, **extra):
    return client.post("/apps/scent-library/fragrance-preview", params=_proxy(), json={"intent": intent, "recommendationId": rec.id, "buildToken": token, "ratios": ratios, **extra})


def _assert_customer_safe(payload: dict, catalog) -> None:
    blob = json.dumps(payload)
    for private in (*PRIVATE, catalog.tag, "Odoo", "odoo", "SKU", "sku", " ml", "warehouse", "Traceback", "Exception"):
        assert private not in blob, (private, blob)
    assert "retry" not in blob.lower() and "seconds" not in blob.lower() and "minutes" not in blob.lower()


@pytest.mark.parametrize("intent", ["save_build", "add_to_cart"])
@pytest.mark.parametrize("failure, code", [("insufficient", "inventory_insufficient"), ("missing_entry", "inventory_unconfirmed"), ("timeout", "inventory_unavailable")])
async def test_preview_actions_fail_closed_with_a_safe_message_and_keep_the_design(catalog, odoo, monkeypatch, intent, failure, code):
    rec = await _two_component_build(catalog, odoo)
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    FAILURE_SETUPS[failure][0](catalog, odoo)
    token = await _token_for(rec)
    with TestClient(app) as client:
        payload = _preview_post(client, rec, token, intent=intent, name="Midnight Rose").json()
    assert payload["code"] == code and payload["error"] == COMMERCE_FAILURES[[k for k, v in COMMERCE_FAILURES.items() if v[1] == code][0]][2]
    assert "status" not in payload and "cartUrl" not in payload and "shopifyVariantId" not in payload and "productUrl" not in payload
    _assert_customer_safe(payload, catalog)
    assert shopify.writes == []
    async with SessionLocal() as session:
        stored = await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == rec.id))
        assert stored.status == "confirmed" and stored.buildStatus == "draft" and not stored.shopifyProductId  # no commerce-success state
        assert stored.draftName == "Midnight Rose" and stored.draftRatiosJson == RATIOS  # the customer's design survives


@pytest.mark.parametrize("intent, status", [("save_build", "saved"), ("add_to_cart", "added")])
async def test_authorized_and_verified_preview_action_completes(catalog, odoo, monkeypatch, intent, status):
    rec = await _two_component_build(catalog, odoo)
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    token = await _token_for(rec)
    with TestClient(app) as client:
        payload = _preview_post(client, rec, token, intent=intent).json()
    assert payload.get("status") == status, payload
    assert "create_product" in shopify.writes and len(odoo.calls) == 1
    if intent == "add_to_cart":
        assert payload["cartUrl"].endswith(":1")  # the backend only ever hands out quantity 1
    _assert_customer_safe({k: v for k, v in payload.items() if k != "productUrl"}, catalog)
    async with SessionLocal() as session:
        stored = await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == rec.id))
        assert stored.buildStatus == "saved" and stored.shopifyProductId == shopify.product_id


async def test_unauthorized_callers_never_reach_inventory_or_shopify(catalog, odoo, monkeypatch):
    rec = await _two_component_build(catalog, odoo)
    other = await catalog.add_recommendation([(1, 60), (2, 40)])
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    other_token = await _token_for(other)
    async with SessionLocal() as session:
        _cid, conversation_token, _cap = await create_conversation_with_capability(session)
    catalog.conversation_ids.append(_cid)
    with TestClient(app) as client:
        attempts = [
            _preview_post(client, rec, None),                      # recommendation id alone
            _preview_post(client, rec, "not-a-real-token-" + "x" * 30),
            _preview_post(client, rec, other_token),               # a valid token for ANOTHER build
            _preview_post(client, rec, conversation_token),        # a conversation token is not build authority
        ]
        for response in attempts:
            assert response.json().get("code") == "build_not_authorized", response.text
        direct = client.post("/api/save-build", json={"recommendationId": rec.id, "buildToken": conversation_token, "ratios": RATIOS})
        assert direct.status_code == 403
        legacy = client.post("/api/save-build", json={"productId": "gid://shopify/Product/1", "ratios": RATIOS, "shop": "attacker.example"})
        assert legacy.status_code == 400
    assert odoo.calls == [] and shopify.calls == []  # knowing an id can never trigger a lookup


async def test_expired_capability_fails_even_when_inventory_would_pass(catalog, odoo, monkeypatch):
    rec = await _two_component_build(catalog, odoo)
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    token = await _token_for(rec)
    async with SessionLocal() as session:
        capability = await session.scalar(select(BuildCapability).where(BuildCapability.recommendationId == rec.id))
        capability.expiresAt = utcnow() - timedelta(minutes=1)
        await session.commit()
    with TestClient(app) as client:
        assert _preview_post(client, rec, token).json().get("code") == "build_not_authorized"
    assert odoo.calls == [] and shopify.calls == []


async def test_browser_supplied_inventory_claims_are_ignored(catalog, odoo, monkeypatch):
    rec = await _two_component_build(catalog, odoo, stock=(0, 0))
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    token = await _token_for(rec)
    claims = {"inventoryVerified": True, "availability": "AVAILABLE", "inventoryStatus": "VERIFIED_AVAILABLE", "checkedAt": "2099-01-01T00:00:00Z",
              "components": [{"sku": "x", "onHandQty": 99999}], "requiredOilMl": 0, "price": "0.01", "productId": "gid://shopify/Product/1", "quantity": 0}
    with TestClient(app) as client:
        payload = _preview_post(client, rec, token, intent="add_to_cart", **claims).json()
    assert payload["code"] == "inventory_insufficient" and shopify.writes == []


@pytest.mark.parametrize("failure, status, code", [("insufficient", 409, "inventory_insufficient"), ("missing_entry", 409, "inventory_unconfirmed"), ("timeout", 503, "inventory_unavailable")])
async def test_direct_save_build_endpoint_fails_closed_with_stable_codes(catalog, odoo, monkeypatch, failure, status, code):
    rec = await _two_component_build(catalog, odoo, shopifyProductId="gid://shopify/Product/555", buildStatus="saved")
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    FAILURE_SETUPS[failure][0](catalog, odoo)
    token = await _token_for(rec)
    with TestClient(app) as client:
        response = client.post("/api/save-build", json={"recommendationId": rec.id, "buildToken": token, "ratios": {"top": 10, "middle": 10, "base": 80}, "name": "New"})
    assert response.status_code == status and response.json()["code"] == code
    assert "Retry-After" not in response.headers and "variantId" not in response.json()
    _assert_customer_safe(response.json(), catalog)
    assert shopify.writes == []


async def test_direct_save_build_endpoint_succeeds_when_verified(catalog, odoo, monkeypatch):
    rec = await _two_component_build(catalog, odoo, shopifyProductId="gid://shopify/Product/555", buildStatus="saved")
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    token = await _token_for(rec)
    with TestClient(app) as client:
        response = client.post("/api/save-build", json={"recommendationId": rec.id, "buildToken": token, "ratios": {"top": 10, "middle": 10, "base": 80}})
    assert response.status_code == 200 and set(response.json()) == {"price", "variantId", "created"}
    assert shopify.writes == ["create_variant"]


async def test_failure_logs_carry_codes_only(catalog, odoo, monkeypatch, caplog):
    rec = await _two_component_build(catalog, odoo, stock=(3.0, 2.0))
    ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    async with SessionLocal() as session:
        with caplog.at_level(logging.INFO), pytest.raises(InventoryNotVerified):
            await require_commerce_inventory(session, recommendation=rec, ratios=RATIOS)
    lines = [r.getMessage() for r in caplog.records if "COMMERCE_INVENTORY_DECISION" in r.getMessage()]
    assert lines and "\"state\": \"INSUFFICIENT\"" in lines[0]
    for line in lines:
        for private in ("FAKE-OIL-", catalog.tag, "3.0", "2.0", "on_hand"):
            assert private not in line


# ===========================================================================
# 6. Concurrency, retries, partial and ambiguous completion
# ===========================================================================

async def test_lock_is_exclusive_per_recommendation_and_released_on_success_and_exception():
    rid = f"{PREFIX}-lock-{uuid.uuid4().hex[:8]}"
    async with build_commerce_lock(rid):
        with pytest.raises(BuildOperationInProgress):
            async with build_commerce_lock(rid):
                pass
        async with build_commerce_lock(rid + "-another-customer"):  # one build never blocks another
            pass
    async with build_commerce_lock(rid):  # released after success
        pass
    with pytest.raises(RuntimeError):
        async with build_commerce_lock(rid):
            raise RuntimeError("boom")
    async with build_commerce_lock(rid):  # released after an exception
        pass


async def test_overlapping_requests_for_one_build_create_exactly_one_product(catalog, odoo, monkeypatch):
    rec = await _two_component_build(catalog, odoo)
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    gate, release = asyncio.Event(), asyncio.Event()

    async def _slow_create(*a, **kw):
        shopify.calls.append("create_product")
        gate.set()
        await release.wait()
        return {"id": shopify.product_id, "handle": "custom-blend"}

    monkeypatch.setattr(builds, "create_product", _slow_create)

    async def _run():
        async with SessionLocal() as session:
            return await execute_build_commerce(session, SHOP, recommendation_id=rec.id, ratios=RATIOS, name="My Blend")

    first = asyncio.create_task(_run())
    await gate.wait()
    with pytest.raises(BuildOperationInProgress):
        await _run()
    release.set()
    assert (await first)["created"] is True
    assert shopify.calls.count("create_product") == 1
    # A sequential retry sees the product that now exists and takes the reprice path instead.
    before = list(shopify.calls)
    await _run()
    assert shopify.calls.count("create_product") == 1 and "read:pricing" in shopify.calls[len(before):]
    # NOTE: this proves one PRODUCT per build in this application. It proves nothing about stock.


@pytest.mark.parametrize("error", [TimeoutError("timed out"), ConnectionError("reset")], ids=["timeout", "transport"])
async def test_ambiguous_creation_is_never_reported_as_success_and_never_blindly_retried(catalog, odoo, monkeypatch, error):
    rec = await _two_component_build(catalog, odoo)
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    shopify.fail_on["create_product"] = error
    token = await _token_for(rec)
    with TestClient(app) as client:
        first = _preview_post(client, rec, token, intent="add_to_cart").json()
        assert first["code"] == "build_pending_review" and "status" not in first and "cartUrl" not in first
        _assert_customer_safe(first, catalog)
        shopify.fail_on.clear()
        second = _preview_post(client, rec, token, intent="add_to_cart").json()  # Shopify is healthy again ...
        assert second["code"] == "build_pending_review"                          # ... but a creation is never repeated blindly
    assert shopify.calls.count("create_product") == 1
    async with SessionLocal() as session:
        stored = await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == rec.id))
        assert stored.buildStatus == "pending_review" and not stored.shopifyProductId and not stored.shopifyVariantId


async def test_product_created_but_price_not_set_is_not_published_and_needs_review(catalog, odoo, monkeypatch):
    rec = await _two_component_build(catalog, odoo)
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    shopify.fail_on["set_variant_price"] = RuntimeError("shopify 502")
    async with SessionLocal() as session:
        with pytest.raises(builds.BuildWriteAmbiguous):
            await execute_build_commerce(session, SHOP, recommendation_id=rec.id, ratios=RATIOS, name="My Blend")
        with pytest.raises(BuildPendingReview):
            await execute_build_commerce(session, SHOP, recommendation_id=rec.id, ratios=RATIOS, name="My Blend")
    assert "publish_to_all_channels" not in shopify.calls and "activate_product" not in shopify.calls and shopify.calls.count("create_product") == 1
    async with SessionLocal() as session:
        stored = await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == rec.id))
        assert stored.buildStatus == "pending_review" and stored.shopifyProductId == shopify.product_id and not stored.shopifyVariantId


async def test_definitive_rejection_and_inventory_refusal_leave_the_build_retryable(catalog, odoo, monkeypatch):
    rec = await _two_component_build(catalog, odoo, stock=(0, 0))
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    async with SessionLocal() as session:
        with pytest.raises(InventoryNotVerified):
            await execute_build_commerce(session, SHOP, recommendation_id=rec.id, ratios=RATIOS, name="My Blend")
        assert (await session.scalar(select(FragranceRecommendation.buildStatus).where(FragranceRecommendation.id == rec.id))) == "draft"
        odoo.stock = {catalog.sku(1): 100.0, catalog.sku(2): 100.0}  # restocked: the same customer can simply try again
        assert (await execute_build_commerce(session, SHOP, recommendation_id=rec.id, ratios=RATIOS, name="My Blend"))["created"] is True
    assert shopify.calls.count("create_product") == 1


async def test_a_crashed_creation_is_treated_as_pending_review(catalog, odoo, monkeypatch):
    rec = await _two_component_build(catalog, odoo, buildStatus="creating")
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    async with SessionLocal() as session:
        with pytest.raises(BuildPendingReview):
            await execute_build_commerce(session, SHOP, recommendation_id=rec.id, ratios=RATIOS, name="My Blend")
    assert shopify.calls == [] and odoo.calls == []


async def test_ambiguous_variant_creation_is_reconciled_by_the_next_read_not_by_a_second_write(catalog, odoo, monkeypatch):
    rec = await _two_component_build(catalog, odoo, shopifyProductId="gid://shopify/Product/555", buildStatus="saved")
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec.id)
    shopify.fail_on["create_variant"] = TimeoutError("timed out")
    target = {"top": 10, "middle": 10, "base": 80}
    async with SessionLocal() as session:
        with pytest.raises(TimeoutError):
            await execute_build_commerce(session, SHOP, recommendation_id=rec.id, ratios=target, name=None)
    # The variant did get created on Shopify's side; the retry READS it and reuses it.
    shopify2 = ShopifyWrites(monkeypatch, recommendation_id=rec.id, existing_variants=[{"node": {
        "id": "gid://shopify/ProductVariant/2", "price": "60.00", "inventoryItem": {"id": "i", "tracked": False},
        "selectedOptions": [{"name": "Top Note", "value": "Rose (10%)"}, {"name": "Middle Note", "value": "Amber (10%)"}, {"name": "Base Note", "value": "Musk (80%)"}]}}])
    async with SessionLocal() as session:
        result = await execute_build_commerce(session, SHOP, recommendation_id=rec.id, ratios=target, name=None)
    assert result["created"] is False and result["variantId"] == "gid://shopify/ProductVariant/2" and "create_variant" not in shopify2.calls


def test_there_is_no_idempotency_key_to_abuse():
    import inspect

    source = inspect.getsource(build_commerce) + inspect.getsource(preview_module)
    assert "idempotency" not in source.lower().replace("there is no idempotency key", "")


# ===========================================================================
# 7. Discovery keeps working; wording is accurate; nothing private leaks
# ===========================================================================

async def test_recommendation_discovery_is_unaffected_by_an_inventory_outage(catalog, odoo):
    await catalog.add_product(1)
    await catalog.add_product(2)
    odoo.response = {"ok": False, "status": None, "error": "timeout"}
    async with SessionLocal() as session:
        lenient = await odoo_inventory.evaluate_candidate_inventory(session, {"recommendationId": "r", "recommendedRatio": [{"productTitle": catalog.title(1), "ratioPercent": 60}, {"productTitle": catalog.title(2), "ratioPercent": 40}]})
    assert lenient["buildable"] is True and lenient["inventoryValidated"] is False  # still a usable recommendation
    safe = safe_views.build_customer_safe_recommendation_from_candidate({"customerFacing": {"customerFacingName": "Test Blend"}, "notes": []}, inventory=lenient, likes=[])
    assert safe.availability == "AVAILABILITY_UNCONFIRMED"


@pytest.mark.parametrize("availability", ["AVAILABLE", "AVAILABILITY_UNCONFIRMED", "UNAVAILABLE"])
def test_model_receives_accurate_availability_wording_and_nothing_private(availability):
    safe = CustomerSafeRecommendation(name="Test Blend", availability=availability)
    content = recommendation_presentation_messages(safe)[1]["content"]
    payload = json.loads(content)
    assert set(payload) == {"recommendation", "availabilityGuidance"} and payload["availabilityGuidance"] == AVAILABILITY_GUIDANCE[availability]
    lowered = payload["availabilityGuidance"].lower()
    assert "do not promise" in lowered
    for private in (*PRIVATE, "odoo", "sku", "warehouse", "quantity", " ml"):
        assert private.lower() not in content.lower(), private
    if availability == "AVAILABLE":
        assert "reserved" in lowered and "guaranteed" in lowered  # told explicitly NOT to claim either


def test_customer_failure_messages_never_claim_success_reservation_or_detail():
    for status, code, message in COMMERCE_FAILURES.values():
        lowered = message.lower()
        assert status in (409, 503) and code.islower()
        for forbidden in ("reserved", "guarantee", "in your cart", "has been created", "sku", "odoo", "warehouse", " ml", "stock level", "units"):
            assert forbidden not in lowered, (code, forbidden)
    assert "your design is saved" in COMMERCE_FAILURES["UNCONFIRMED"][2].lower()


async def test_preview_page_data_carries_no_inventory_information(catalog, odoo):
    rec = await _two_component_build(catalog, odoo)
    token = await _token_for(rec)
    with TestClient(app) as client:
        page = client.get("/apps/scent-library/fragrance-preview", params=_proxy(recommendationId=rec.id, bt=token))
    assert page.status_code == 200
    for private in (*PRIVATE, catalog.title(1), catalog.sku(1), "availability", "inventory"):
        assert private not in page.text, private
    assert odoo.calls == []  # viewing a preview is not a commerce action and costs no lookup


async def test_chat_pipeline_never_calls_the_commerce_gate(monkeypatch):
    import inspect

    assert "commerce_inventory" not in inspect.getsource(conversation_flow)
    from app.ai import tool_executor
    from app.services import legacy_preview_recovery, recommendation_confirmation, recommendation_pipeline

    for module in (tool_executor, recommendation_pipeline, recommendation_confirmation, legacy_preview_recovery):
        source = inspect.getsource(module)
        assert "create_shopify_build_product" not in source and "reprice_existing_build" not in source and "create_variant" not in source, module.__name__
