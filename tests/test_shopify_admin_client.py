import httpx
import pytest

from app.shopify import admin_client
from app.shopify.admin_client import ShopNotAuthenticated, admin_graphql


class _FakeDbSession:
    def __init__(self, token):
        self._token = token

    async def scalar(self, *a, **kw):
        return None  # unused directly; get_offline_access_token is monkeypatched below


async def test_admin_graphql_raises_when_shop_has_no_stored_token(monkeypatch):
    monkeypatch.setattr(admin_client, "get_offline_access_token", lambda session, shop: _async(None))
    with pytest.raises(ShopNotAuthenticated):
        await admin_graphql(_FakeDbSession(None), "test-shop.myshopify.com", "query { shop { name } }")


async def test_admin_graphql_posts_with_token_header_and_returns_json(monkeypatch):
    monkeypatch.setattr(admin_client, "get_offline_access_token", lambda session, shop: _async("shpat_real_token"))

    captured = {}

    async def _fake_post(url, token, query, variables):
        captured.update(url=url, token=token, query=query, variables=variables)
        return httpx.Response(200, request=httpx.Request("POST", url), json={"data": {"shop": {"name": "Test Shop"}}})

    monkeypatch.setattr(admin_client, "_post", _fake_post)

    result = await admin_graphql(_FakeDbSession(None), "test-shop.myshopify.com", "query { shop { name } }", {"x": 1})

    assert result == {"data": {"shop": {"name": "Test Shop"}}}
    assert captured["token"] == "shpat_real_token"
    assert "test-shop.myshopify.com/admin/api/" in captured["url"]
    assert captured["variables"] == {"x": 1}


async def test_admin_graphql_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(admin_client, "get_offline_access_token", lambda session, shop: _async("shpat_real_token"))

    async def _fake_post(url, token, query, variables):
        return httpx.Response(401, request=httpx.Request("POST", url), json={"errors": "invalid token"})

    monkeypatch.setattr(admin_client, "_post", _fake_post)

    with pytest.raises(httpx.HTTPStatusError):
        await admin_graphql(_FakeDbSession(None), "test-shop.myshopify.com", "query { shop { name } }")


async def _async(value):
    return value
