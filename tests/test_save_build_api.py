"""Covers app/api/save_build.py -- the direct, non-App-Proxy /api/save-build endpoint the live
custom-scent-product.liquid theme's note sliders call. Mirrors test_preview.py's
add_to_cart/save_build coverage but without a recommendationId or App Proxy signature, matching
this endpoint's actual (simpler) contract.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import save_build as save_build_module
from app.main import app
from app.shopify.admin_client import ShopNotAuthenticated
from app.shopify.builds import InvalidComputedPrice, ProductPricingNotFound

ORIGIN = "https://test-3d-products.myshopify.com"


def test_save_build_resolves_existing_variant(monkeypatch):
    async def _fake_reprice(session, shop, *, product_id, ratios, name=None):
        assert shop == "test-3d-products.myshopify.com"
        assert product_id == "gid://shopify/Product/1"
        return {"price": "60.00", "variantId": "gid://shopify/ProductVariant/42", "created": False}

    monkeypatch.setattr(save_build_module, "reprice_existing_build", _fake_reprice)

    with TestClient(app) as client:
        response = client.post(
            "/api/save-build", headers={"Origin": ORIGIN},
            json={"productId": "gid://shopify/Product/1", "ratios": {"top": 40, "middle": 30, "base": 30}},
        )
    assert response.status_code == 200
    assert response.json() == {"price": "60.00", "variantId": "gid://shopify/ProductVariant/42", "created": False}
    assert response.headers["access-control-allow-origin"] == "*"


def test_save_build_maps_missing_note_composition_to_404(monkeypatch):
    async def _fake_reprice(*a, **kw):
        raise ProductPricingNotFound("Could not find product or its note composition.")

    monkeypatch.setattr(save_build_module, "reprice_existing_build", _fake_reprice)

    with TestClient(app) as client:
        response = client.post(
            "/api/save-build", headers={"Origin": ORIGIN},
            json={"productId": "gid://shopify/Product/1", "ratios": {"top": 100}},
        )
    assert response.status_code == 404
    assert "note composition" in response.json()["error"]


def test_save_build_maps_stale_shopify_token_to_customer_safe_401(monkeypatch):
    # The exact live bug this endpoint replaces the old Node route to fix: a stored token
    # Shopify itself rejects. Must surface as a clear, customer-safe "reconnect the app" message,
    # not a raw 500 or an internal auth detail.
    async def _fake_reprice(*a, **kw):
        request = httpx.Request("POST", "https://test-3d-products.myshopify.com/admin/api/graphql.json")
        response = httpx.Response(401, request=request, json={"errors": "Unauthorized"})
        raise httpx.HTTPStatusError("401", request=request, response=response)

    monkeypatch.setattr(save_build_module, "reprice_existing_build", _fake_reprice)

    with TestClient(app) as client:
        response = client.post(
            "/api/save-build", headers={"Origin": ORIGIN},
            json={"productId": "gid://shopify/Product/1", "ratios": {"top": 100}},
        )
    assert response.status_code == 401
    assert "reconnect the app" in response.json()["error"]


def test_save_build_reports_no_admin_credential_as_customer_safe_401(monkeypatch):
    async def _fake_reprice(*a, **kw):
        raise ShopNotAuthenticated("no usable Admin API credential")

    monkeypatch.setattr(save_build_module, "reprice_existing_build", _fake_reprice)

    with TestClient(app) as client:
        response = client.post(
            "/api/save-build", headers={"Origin": ORIGIN},
            json={"productId": "gid://shopify/Product/1", "ratios": {"top": 100}},
        )
    assert response.status_code == 401
    assert "reconnect the app" in response.json()["error"]


def test_save_build_maps_invalid_price_to_400(monkeypatch):
    async def _fake_reprice(*a, **kw):
        raise InvalidComputedPrice("Computed price was invalid.")

    monkeypatch.setattr(save_build_module, "reprice_existing_build", _fake_reprice)

    with TestClient(app) as client:
        response = client.post(
            "/api/save-build", headers={"Origin": ORIGIN},
            json={"productId": "gid://shopify/Product/1", "ratios": {"top": 100}},
        )
    assert response.status_code == 400


def test_save_build_preflight_returns_cors_headers():
    with TestClient(app) as client:
        response = client.options("/api/save-build")
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "*"
