"""Phase 1 regression tests for F2 / N1 / N3 on the direct /api/save-build endpoint and the
build orchestration in app/shopify/builds.py.

Everything Shopify-facing is mocked at the admin_graphql boundary and every write is COUNTED:
an attack is closed only if the number of Shopify writes is zero, not merely because the
endpoint returned an error status. No database is needed: the session dependency is overridden
and the recommendation / capability lookups are stubbed."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.services import build_commerce
from app.api import save_build as save_build_module
from app.config import settings
from app.db.session import get_session
from app.main import app
from app.services.build_capability import BuildNotAuthorized
from app.shopify import builds, products
from app.shopify.builds import InvalidComputedPrice, reprice_existing_build

# Phase 5: these tests are about other invariants; the inventory gate has its own suite.
pytestmark = pytest.mark.usefixtures("inventory_verified")

TRUSTED = "test-shop.myshopify.com"
TRUSTED_ORIGIN = f"https://{TRUSTED}"
REC_ID = "rec-authorized-0001"
OTHER_REC_ID = "rec-someone-else-0002"
PRODUCT_GID = "gid://shopify/Product/1001"
GOOD_TOKEN = "good-token-for-rec-0001"
WRITE_MUTATIONS = ("productUpdate", "productVariantsBulkCreate", "productVariantsBulkUpdate", "inventoryItemUpdate", "productCreate", "productCreateMedia", "publishablePublish")


def _recommendation(**overrides):
    defaults = dict(
        id=REC_ID, conversationId="conv-1", shopifyProductId=PRODUCT_GID, shopifyVariantId="gid://shopify/ProductVariant/1",
        buildStatus="saved", combinationType="HYBRID",
        productsJson=[{"title": "A", "notes": ["Rose"], "contribution": "x"}], customerProfileJson={}, ratiosJson=[],
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _product(**overrides):
    base = {
        "id": PRODUCT_GID, "title": "Old Title", "vendor": "The Dua Brand", "templateSuffix": "custom-scent",
        "metafield": {"value": json.dumps({"recommendationId": REC_ID, "combinationType": "HYBRID", "layers": [
            {"position": "top", "quantityMl": 17.0, "pricePer5ml": 20},
            {"position": "middle", "quantityMl": 8.5, "pricePer5ml": 20},
            {"position": "base", "quantityMl": 8.5, "pricePer5ml": 20},
        ]})},
        "variants": {"edges": [{"node": {"id": "gid://shopify/ProductVariant/1", "price": "136.00",
                                          "selectedOptions": [{"name": "Top Note", "value": "Rose (50%)"}, {"name": "Middle Note", "value": "Amber (25%)"}, {"name": "Base Note", "value": "Musk (25%)"}],
                                          "inventoryItem": {"id": "gid://shopify/InventoryItem/1", "tracked": False}}}]},
    }
    base.update(overrides)
    return base


class _FakeSession:
    async def scalar(self, *a, **kw):
        raise AssertionError("unexpected database access")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(settings, "shopify_shop_domain", TRUSTED)
    monkeypatch.setattr(settings, "allowed_origins", "")

    async def _fake_get_session():
        yield _FakeSession()

    app.dependency_overrides[get_session] = _fake_get_session
    yield
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture
def shopify(monkeypatch):
    """Mocked Admin GraphQL boundary that records every call and serves a product for PRODUCT_GID."""
    state = {"calls": [], "product": _product()}

    async def _fake_admin_graphql(session, shop, query, variables=None):
        assert shop == TRUSTED, f"Admin call for a non-trusted shop: {shop!r}"
        name = " ".join(query.split())
        state["calls"].append({"query": name, "variables": variables})
        if "getProductForPricing" in name:
            return {"data": {"product": state["product"] if variables["id"] == state["product"]["id"] else None}}
        if "productUpdate" in name:
            return {"data": {"productUpdate": {"userErrors": []}}}
        if "productVariantsBulkCreate" in name:
            return {"data": {"productVariantsBulkCreate": {"userErrors": [], "productVariants": [{"id": "gid://shopify/ProductVariant/2", "price": variables["variants"][0]["price"]}]}}}
        if "inventoryItemUpdate" in name:
            return {"data": {"inventoryItemUpdate": {"userErrors": []}}}
        return {"data": {}}

    monkeypatch.setattr(products, "admin_graphql", _fake_admin_graphql)
    state["writes"] = lambda: [c for c in state["calls"] if any(m in c["query"] for m in WRITE_MUTATIONS)]
    state["reads"] = lambda: [c for c in state["calls"] if c["query"].startswith("query")]
    return state


@pytest.fixture
def capability(monkeypatch):
    """Stubbed capability store: GOOD_TOKEN authorizes REC_ID, nothing else."""
    async def _authorize(session, *, token, recommendation_id):
        if token == GOOD_TOKEN and recommendation_id == REC_ID:
            return SimpleNamespace(recommendationId=REC_ID)
        raise BuildNotAuthorized()

    monkeypatch.setattr(save_build_module, "authorize_build_token", _authorize)


@pytest.fixture
def recommendations(monkeypatch):
    store = {REC_ID: _recommendation(), OTHER_REC_ID: _recommendation(id=OTHER_REC_ID, shopifyProductId="gid://shopify/Product/2002")}

    async def _get(session, recommendation_id):
        return store.get(recommendation_id)

    monkeypatch.setattr(save_build_module, "get_recommendation", _get)
    monkeypatch.setattr(build_commerce, "get_recommendation", _get)
    return store


def _post(client, body, origin=TRUSTED_ORIGIN):
    headers = {"Origin": origin} if origin is not None else {}
    return client.post("/api/save-build", headers=headers, json=body)


VALID_BODY = {"recommendationId": REC_ID, "buildToken": GOOD_TOKEN, "ratios": {"top": 10, "middle": 10, "base": 80}, "name": "New Name"}


# ---------------------------------------------------------------------------
# F2: arbitrary product GIDs and the old contract
# ---------------------------------------------------------------------------

def test_old_contract_with_arbitrary_product_gid_is_refused_with_zero_shopify_calls(shopify, capability, recommendations):
    with TestClient(app) as client:
        response = _post(client, {"productId": "gid://shopify/Product/424242", "ratios": {"top": 40, "middle": 30, "base": 30}, "name": "PWNED"})
    assert response.status_code == 400
    assert response.json()["code"] == "build_contract_upgraded"
    assert shopify["calls"] == []


def test_product_gid_smuggled_alongside_the_new_fields_is_rejected(shopify, capability, recommendations):
    with TestClient(app) as client:
        response = _post(client, {**VALID_BODY, "productId": "gid://shopify/Product/424242"})
    assert response.status_code == 400
    assert shopify["calls"] == []


def test_origin_header_no_longer_selects_the_shop(shopify, capability, recommendations, monkeypatch):
    # Even with a valid capability, an attacker Origin cannot redirect Admin calls: the mocked
    # admin_graphql asserts shop == TRUSTED, and the unknown origin is refused outright.
    with TestClient(app) as client:
        response = _post(client, VALID_BODY, origin="https://attacker.example.com")
    assert response.status_code == 403
    assert shopify["calls"] == []


# ---------------------------------------------------------------------------
# N3: capability / ownership
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {**VALID_BODY, "buildToken": "wrong-token"},
    {**VALID_BODY, "buildToken": ""},
    {**VALID_BODY, "recommendationId": OTHER_REC_ID},              # token for A used on B
    {k: v for k, v in VALID_BODY.items() if k != "buildToken"},   # id alone
    {**VALID_BODY, "buildToken": GOOD_TOKEN * 10},
])
def test_unauthorized_capability_never_reaches_shopify(shopify, capability, recommendations, body):
    with TestClient(app) as client:
        response = _post(client, body)
    assert response.status_code in (400, 403)
    assert shopify["calls"] == []


def test_recommendation_id_alone_is_not_authorization(shopify, capability, recommendations):
    with TestClient(app) as client:
        response = _post(client, {"recommendationId": REC_ID, "buildToken": "", "ratios": {"top": 34, "middle": 33, "base": 33}})
    assert response.status_code == 403
    assert response.json()["code"] == "build_not_authorized"
    assert shopify["calls"] == []


# ---------------------------------------------------------------------------
# Product identity is verified before any write
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("product_override", [
    {"metafield": None},
    {"metafield": {"value": ""}},
    {"metafield": {"value": "not json"}},
    {"metafield": {"value": json.dumps({"recommendationId": OTHER_REC_ID, "layers": [{"position": "top"}, {"position": "middle"}, {"position": "base"}]})}},
    {"metafield": {"value": json.dumps({"recommendationId": REC_ID, "layers": []})}},
    {"metafield": {"value": json.dumps({"recommendationId": REC_ID, "layers": [{"position": "top", "quantityMl": 34, "pricePer5ml": 20}]})}},
    {"vendor": "Someone Else"},
    {"templateSuffix": None},
    {"templateSuffix": "default"},
    {"id": "gid://shopify/Product/9999"},
    {"variants": {"edges": []}},
])
def test_product_that_is_not_this_recommendations_build_is_never_written(shopify, capability, recommendations, product_override):
    shopify["product"] = _product(**product_override)
    with TestClient(app) as client:
        response = _post(client, VALID_BODY)
    assert response.status_code == 404
    assert shopify["writes"]() == []
    # Exactly one read happened and nothing else.
    assert [c["query"].startswith("query getProductForPricing") for c in shopify["calls"]] == [True]


def test_db_product_id_mismatch_is_never_written(shopify, capability, recommendations):
    recommendations[REC_ID].shopifyProductId = "gid://shopify/Product/424242"  # DB says a different product
    with TestClient(app) as client:
        response = _post(client, VALID_BODY)
    assert response.status_code == 404
    assert shopify["writes"]() == []


def test_recommendation_without_a_saved_product_cannot_be_repriced(shopify, capability, recommendations):
    recommendations[REC_ID].shopifyProductId = None
    with TestClient(app) as client:
        response = _post(client, VALID_BODY)
    assert response.status_code == 409
    assert shopify["calls"] == []


# ---------------------------------------------------------------------------
# N1: price manipulation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ratios", [
    {"top": 1, "middle": 1, "base": 1},
    {"top": 0, "middle": 0, "base": 0},
    {"top": -100, "middle": 100, "base": 100},
    {"top": 100, "middle": 100, "base": -100},
    {"top": 150, "middle": -25, "base": -25},
    {"top": 101, "middle": 0, "base": -1},
    {"top": 40, "middle": 30},
    {"top": 40, "middle": 30, "base": 30, "extra": 1},
    {"top": "NaN", "middle": 50, "base": 50},
    {"top": 1e308, "middle": 1e308, "base": 1e308},
    {"top": "40", "middle": "30", "base": "30"},
    "40/30/30",
    None,
])
def test_invalid_ratios_are_rejected_before_any_shopify_call(shopify, capability, recommendations, ratios):
    with TestClient(app) as client:
        response = _post(client, {**VALID_BODY, "ratios": ratios})
    assert response.status_code == 400
    assert response.json()["code"] in ("invalid_ratios", "invalid_request")
    assert shopify["calls"] == []


def test_json_nan_and_infinity_literals_are_rejected_before_any_shopify_call(shopify, capability, recommendations):
    # Python's json module accepts bare NaN/Infinity literals; pydantic's float would too.
    for literal in ("NaN", "Infinity", "-Infinity"):
        raw = '{"recommendationId": "%s", "buildToken": "%s", "ratios": {"top": %s, "middle": 50, "base": 50}}' % (REC_ID, GOOD_TOKEN, literal)
        with TestClient(app) as client:
            response = client.post("/api/save-build", headers={"Origin": TRUSTED_ORIGIN, "Content-Type": "application/json"}, content=raw)
        assert response.status_code == 400
    assert shopify["calls"] == []


def test_price_cannot_be_reduced_by_an_incomplete_composition():
    # Direct orchestration-level check: only a full 100% composition reaches the price math.
    with pytest.raises(builds.InvalidRatios):
        asyncio.run(reprice_existing_build(_FakeSession(), TRUSTED, recommendation=_recommendation(), ratios={"top": 1, "middle": 1, "base": 1}))


def test_non_finite_or_non_positive_computed_price_is_refused(monkeypatch):
    monkeypatch.setattr(builds, "get_product_for_pricing", lambda *a, **kw: _async(_product(metafield={"value": json.dumps({"recommendationId": REC_ID, "layers": [
        {"position": "top", "quantityMl": 0, "pricePer5ml": 0}, {"position": "middle", "quantityMl": 0, "pricePer5ml": 0}, {"position": "base", "quantityMl": 0, "pricePer5ml": 0},
    ]})})))
    writes = []
    monkeypatch.setattr(builds, "create_variant", lambda *a, **kw: writes.append("create") or _async({"id": "v", "price": "0"}))
    monkeypatch.setattr(builds, "rename_product", lambda *a, **kw: writes.append("rename") or _async(None))
    with pytest.raises(InvalidComputedPrice):
        asyncio.run(reprice_existing_build(_FakeSession(), TRUSTED, recommendation=_recommendation(), ratios={"top": 34, "middle": 33, "base": 33}))
    assert writes == []


# ---------------------------------------------------------------------------
# Mutation ordering and the valid flow
# ---------------------------------------------------------------------------

def test_valid_request_reads_verifies_then_writes_and_renames_last(shopify, capability, recommendations):
    with TestClient(app) as client:
        response = _post(client, VALID_BODY)
    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True and body["variantId"] == "gid://shopify/ProductVariant/2"
    # Server-computed price from the product's own layer data at 10/10/80 of a $136 bottle.
    assert body["price"] == "136.00"
    order = [c["query"].split("(")[0] for c in shopify["calls"]]
    assert order[0].startswith("query getProductForPricing")
    assert order[1].startswith("mutation createBuildVariant")
    assert order[2].startswith("mutation renameBuildProduct")
    assert len(order) == 3
    assert response.headers["access-control-allow-origin"] == TRUSTED_ORIGIN
    assert "Origin" in response.headers["vary"]


def test_matching_variant_is_reused_and_rename_is_still_last(shopify, capability, recommendations):
    with TestClient(app) as client:
        response = _post(client, {**VALID_BODY, "ratios": {"top": 51, "middle": 24, "base": 25}})
    assert response.status_code == 200
    assert response.json() == {"price": "136.00", "variantId": "gid://shopify/ProductVariant/1", "created": False}
    assert [c["query"].split("(")[0] for c in shopify["calls"]] == ["query getProductForPricing", "mutation renameBuildProduct"]


def test_unchanged_or_missing_name_skips_the_rename_entirely(shopify, capability, recommendations):
    with TestClient(app) as client:
        assert _post(client, {**VALID_BODY, "name": "Old Title"}).status_code == 200
        assert _post(client, {k: v for k, v in VALID_BODY.items() if k != "name"}).status_code == 200
    assert not any("renameBuildProduct" in c["query"] for c in shopify["calls"])


@pytest.mark.parametrize("name", ["x" * 5000, "Bad\x00Name", "Zero​Width", 123])
def test_invalid_names_are_rejected_before_any_shopify_call(shopify, capability, recommendations, name):
    with TestClient(app) as client:
        response = _post(client, {**VALID_BODY, "name": name})
    assert response.status_code == 400
    assert shopify["calls"] == []


@pytest.mark.parametrize("ratios", [{"top": 34, "middle": 33, "base": 33}, {"top": 50, "middle": 25, "base": 25}, {"top": 40.0, "middle": 30.0, "base": 30.0}])
def test_normal_compositions_still_work(shopify, capability, recommendations, ratios):
    with TestClient(app) as client:
        response = _post(client, {**VALID_BODY, "ratios": ratios})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# CORS: exact trusted origins only, never a wildcard
# ---------------------------------------------------------------------------

def test_preflight_from_the_trusted_storefront_is_allowed_without_wildcard(shopify, capability, recommendations):
    # A real browser preflight (with Access-Control-Request-Method) is answered by the global
    # TrustedOriginCORSMiddleware; a bare OPTIONS reaches the route. Both reflect the exact origin.
    with TestClient(app) as client:
        real_preflight = client.options("/api/save-build", headers={"Origin": TRUSTED_ORIGIN, "Access-Control-Request-Method": "POST"})
        bare_options = client.options("/api/save-build", headers={"Origin": TRUSTED_ORIGIN})
    for response in (real_preflight, bare_options):
        assert response.status_code in (200, 204)
        assert response.headers["access-control-allow-origin"] == TRUSTED_ORIGIN
        assert "Origin" in response.headers["vary"]


def test_configured_custom_storefront_origin_is_allowed(shopify, capability, recommendations, monkeypatch):
    monkeypatch.setattr(settings, "allowed_origins", "https://www.example-storefront.com")
    with TestClient(app) as client:
        response = client.options("/api/save-build", headers={"Origin": "https://www.example-storefront.com"})
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "https://www.example-storefront.com"


@pytest.mark.parametrize("origin", ["https://attacker.example.com", "https://test-shop.myshopify.com.attacker.com", "null", "http://test-shop.myshopify.com", "*"])
def test_unknown_origins_get_no_cors_headers_and_are_refused(shopify, capability, recommendations, origin):
    with TestClient(app) as client:
        real_preflight = client.options("/api/save-build", headers={"Origin": origin, "Access-Control-Request-Method": "POST"})
        bare_options = client.options("/api/save-build", headers={"Origin": origin})
        post = _post(client, VALID_BODY, origin=origin)
    assert real_preflight.status_code in (400, 403) and "access-control-allow-origin" not in real_preflight.headers
    assert bare_options.status_code == 403 and "access-control-allow-origin" not in bare_options.headers
    assert post.status_code == 403 and "access-control-allow-origin" not in post.headers
    assert shopify["calls"] == []


def test_wildcard_is_never_returned(shopify, capability, recommendations):
    with TestClient(app) as client:
        for response in (client.options("/api/save-build", headers={"Origin": TRUSTED_ORIGIN}), _post(client, VALID_BODY)):
            assert response.headers.get("access-control-allow-origin") != "*"


def test_request_without_origin_is_still_fully_authorized_server_side(shopify, capability, recommendations):
    # CORS is not authorization: a non-browser caller with no Origin still needs the capability.
    with TestClient(app) as client:
        denied = _post(client, {**VALID_BODY, "buildToken": "wrong"}, origin=None)
        assert denied.status_code == 403 and shopify["calls"] == []
        allowed = _post(client, VALID_BODY, origin=None)
    assert allowed.status_code == 200
    assert "access-control-allow-origin" not in allowed.headers


async def _async(value):
    return value
