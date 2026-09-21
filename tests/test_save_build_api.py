"""Covers app/api/save_build.py -- the direct, non-App-Proxy /api/save-build endpoint the
storefront theme's note sliders call.

Phase 1 (security): this file used to assert the vulnerable contract -- shop taken from the
browser's Origin header, an arbitrary caller-supplied productId mutated, and
`Access-Control-Allow-Origin: *`. Those assertions enshrined F1/F2/N1 (see docs/SECURITY_AUDIT.md)
and were replaced, not weakened: the endpoint now requires a recommendationId plus its build
capability token, takes the product id from server state, and reflects only trusted origins.
The full attack matrix (arbitrary GIDs, foreign tokens, price manipulation, CORS) is in
tests/security/test_save_build_authorization.py; this file keeps the error-mapping coverage.
"""

from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.services import build_commerce
from app.api import save_build as save_build_module
from app.config import settings
from app.db.session import get_session
from app.main import app
from app.shopify.admin_client import ShopNotAuthenticated
from app.shopify.builds import InvalidComputedPrice, ProductPricingNotFound

SHOP = "test-shop.myshopify.com"
ORIGIN = f"https://{SHOP}"
REC_ID = "rec-pytest-save-build"
TOKEN = "fake-build-token-for-tests"
BODY = {"recommendationId": REC_ID, "buildToken": TOKEN, "ratios": {"top": 40, "middle": 30, "base": 30}}


class _FakeSession:
    async def scalar(self, *a, **kw):
        raise AssertionError("unexpected database access")


@pytest.fixture(autouse=True)
def _authorized_build(monkeypatch):
    monkeypatch.setattr(settings, "shopify_shop_domain", SHOP)

    async def _fake_get_session():
        yield _FakeSession()

    async def _authorize(session, *, token, recommendation_id):
        assert token == TOKEN and recommendation_id == REC_ID
        return SimpleNamespace(recommendationId=REC_ID)

    async def _get_recommendation(session, recommendation_id):
        return SimpleNamespace(id=REC_ID, shopifyProductId="gid://shopify/Product/1", buildStatus="saved")

    app.dependency_overrides[get_session] = _fake_get_session
    monkeypatch.setattr(save_build_module, "authorize_build_token", _authorize)
    monkeypatch.setattr(save_build_module, "get_recommendation", _get_recommendation)
    monkeypatch.setattr(build_commerce, "get_recommendation", _get_recommendation)
    yield
    app.dependency_overrides.pop(get_session, None)


def _post(client, body=BODY):
    return client.post("/api/save-build", headers={"Origin": ORIGIN}, json=body)


def test_save_build_resolves_existing_variant(monkeypatch):
    async def _fake_reprice(session, shop, *, recommendation, ratios, name=None):
        # The shop is the configured trusted shop and the product comes from the recommendation
        # row -- never from the request.
        assert shop == SHOP
        assert recommendation.shopifyProductId == "gid://shopify/Product/1"
        assert ratios == {"top": 40, "middle": 30, "base": 30}
        return {"price": "60.00", "variantId": "gid://shopify/ProductVariant/42", "created": False}

    monkeypatch.setattr(build_commerce, "reprice_existing_build", _fake_reprice)

    with TestClient(app) as client:
        response = _post(client)
    assert response.status_code == 200
    assert response.json() == {"price": "60.00", "variantId": "gid://shopify/ProductVariant/42", "created": False}
    # Exact trusted origin, never a wildcard, on a privileged mutation response.
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "Origin" in response.headers["vary"]


def test_save_build_maps_invalid_build_product_to_404(monkeypatch):
    async def _fake_reprice(*a, **kw):
        raise ProductPricingNotFound("Could not find product or its note composition.")

    monkeypatch.setattr(build_commerce, "reprice_existing_build", _fake_reprice)

    with TestClient(app) as client:
        response = _post(client)
    assert response.status_code == 404
    assert "note composition" in response.json()["error"]


def test_save_build_maps_stale_shopify_token_to_customer_safe_401(monkeypatch):
    # A stored token Shopify itself rejects must surface as a clear, customer-safe "reconnect the
    # app" message, not a raw 500 or an internal auth detail.
    async def _fake_reprice(*a, **kw):
        request = httpx.Request("POST", f"https://{SHOP}/admin/api/graphql.json")
        response = httpx.Response(401, request=request, json={"errors": "Unauthorized"})
        raise httpx.HTTPStatusError("401", request=request, response=response)

    monkeypatch.setattr(build_commerce, "reprice_existing_build", _fake_reprice)

    with TestClient(app) as client:
        response = _post(client)
    assert response.status_code == 401
    assert "reconnect the app" in response.json()["error"]


def test_save_build_reports_no_admin_credential_as_customer_safe_401(monkeypatch):
    async def _fake_reprice(*a, **kw):
        raise ShopNotAuthenticated("no usable Admin API credential")

    monkeypatch.setattr(build_commerce, "reprice_existing_build", _fake_reprice)

    with TestClient(app) as client:
        response = _post(client)
    assert response.status_code == 401
    assert "reconnect the app" in response.json()["error"]


def test_save_build_maps_invalid_price_to_400(monkeypatch):
    async def _fake_reprice(*a, **kw):
        raise InvalidComputedPrice("Computed price was invalid.")

    monkeypatch.setattr(build_commerce, "reprice_existing_build", _fake_reprice)

    with TestClient(app) as client:
        response = _post(client)
    assert response.status_code == 400


def test_save_build_preflight_returns_exact_origin_not_wildcard():
    with TestClient(app) as client:
        response = client.options("/api/save-build", headers={"Origin": ORIGIN})
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "Origin" in response.headers["vary"]


def test_save_build_refuses_to_run_without_a_configured_trusted_shop(monkeypatch):
    # No silent fallback to a test store: an unconfigured deployment fails closed.
    monkeypatch.setattr(settings, "shopify_shop_domain", "")
    called = False

    async def _fake_reprice(*a, **kw):
        nonlocal called
        called = True

    monkeypatch.setattr(build_commerce, "reprice_existing_build", _fake_reprice)
    with TestClient(app) as client:
        response = client.post("/api/save-build", json=BODY)
    assert response.status_code == 503
    assert called is False
