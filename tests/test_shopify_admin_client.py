import httpx
import pytest

from app.shopify import admin_client
from app.shopify.admin_client import ShopNotAuthenticated, admin_graphql

# Must equal the suite-wide trusted shop (tests/conftest.py); any other value is refused before
# any credential lookup or HTTP request -- see tests/security/test_trusted_shop.py.
SHOP = "test-shop.myshopify.com"


class _FakeDbSession:
    async def scalar(self, *a, **kw):
        return None  # unused directly; get_admin_access_token is monkeypatched below


async def test_admin_graphql_raises_when_shop_has_no_usable_credential(monkeypatch):
    # These three tests used to patch admin_client.get_offline_access_token, which stopped
    # existing when auth moved into admin_auth.py (commit 42f2bf2) -- they had been failing on
    # main ever since. Patch the boundary admin_client actually calls.
    monkeypatch.setattr(admin_client, "get_admin_access_token", lambda session, shop: _async((None, "none")))
    with pytest.raises(ShopNotAuthenticated):
        await admin_graphql(_FakeDbSession(), SHOP, "query { shop { name } }")


async def test_admin_graphql_posts_with_token_header_and_returns_json(monkeypatch):
    monkeypatch.setattr(admin_client, "get_admin_access_token", lambda session, shop: _async(("fake-admin-token", "client_credentials")))

    captured = {}

    async def _fake_post(url, token, query, variables):
        captured.update(url=url, token=token, query=query, variables=variables)
        return httpx.Response(200, request=httpx.Request("POST", url), json={"data": {"shop": {"name": "Test Shop"}}})

    monkeypatch.setattr(admin_client, "_post", _fake_post)

    result = await admin_graphql(_FakeDbSession(), SHOP, "query { shop { name } }", {"x": 1})

    assert result == {"data": {"shop": {"name": "Test Shop"}}}
    assert captured["token"] == "fake-admin-token"
    assert captured["url"].startswith(f"https://{SHOP}/admin/api/")
    assert captured["variables"] == {"x": 1}


async def test_admin_graphql_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(admin_client, "get_admin_access_token", lambda session, shop: _async(("fake-admin-token", "client_credentials")))

    async def _fake_post(url, token, query, variables):
        return httpx.Response(401, request=httpx.Request("POST", url), json={"errors": "invalid token"})

    monkeypatch.setattr(admin_client, "_post", _fake_post)

    with pytest.raises(httpx.HTTPStatusError):
        await admin_graphql(_FakeDbSession(), SHOP, "query { shop { name } }")


async def _async(value):
    return value
