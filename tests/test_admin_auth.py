import logging

import httpx
import pytest

from app.config import settings
from app.shopify import admin_auth

# Must equal the suite-wide trusted shop (tests/conftest.py) -- Phase 1 refuses any other value
# before any HTTP request; see tests/security/test_trusted_shop.py.
SHOP = "test-shop.myshopify.com"


@pytest.fixture(autouse=True)
def _clear_cache():
    admin_auth._token_cache.clear()
    yield
    admin_auth._token_cache.clear()


@pytest.fixture(autouse=True)
def _no_real_credentials_by_default(monkeypatch):
    # Most tests explicitly set these where needed; keep the default empty so a test that forgets
    # to configure them fails loudly instead of silently hitting the real Shopify API.
    monkeypatch.setattr(settings, "shopify_api_key", "")
    monkeypatch.setattr(settings, "shopify_api_secret", "")


async def test_returns_none_source_when_nothing_is_configured_or_stored(monkeypatch):
    async def _no_session_token(*_a, **_kw):
        return None

    monkeypatch.setattr("app.shopify.sessions.get_offline_access_token", _no_session_token)

    token, source = await admin_auth.get_admin_access_token(session=None, shop=SHOP)
    assert token is None
    assert source == "none"


async def test_client_credentials_not_attempted_when_unconfigured(monkeypatch):
    # If SHOPIFY_API_KEY/SECRET aren't set, the grant request must never even be sent -- falls
    # straight through to the Session-table fallback.
    called = False

    async def _fake_post(*_a, **_kw):
        nonlocal called
        called = True
        raise AssertionError("should never attempt the client credentials request when unconfigured")

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    async def _session_token(*_a, **_kw):
        return "fake-session-token"

    monkeypatch.setattr("app.shopify.sessions.get_offline_access_token", _session_token)

    token, source = await admin_auth.get_admin_access_token(session=None, shop=SHOP)
    assert called is False
    assert token == "fake-session-token"
    assert source == "session_table"


async def test_client_credentials_success_is_used_and_cached(monkeypatch, caplog):
    monkeypatch.setattr(settings, "shopify_api_key", "test-client-id")
    monkeypatch.setattr(settings, "shopify_api_secret", "test-client-secret")

    call_count = 0

    async def _fake_request(self, url, json=None, **kwargs):
        nonlocal call_count
        call_count += 1
        request = httpx.Request("POST", url)
        return httpx.Response(200, request=request, json={"access_token": "real-shopify-token", "scope": "write_products", "expires_in": 3600})

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_request)

    with caplog.at_level(logging.INFO, logger="app.shopify.admin_auth"):
        token1, source1 = await admin_auth.get_admin_access_token(session=None, shop=SHOP)
        token2, source2 = await admin_auth.get_admin_access_token(session=None, shop=SHOP)

    assert token1 == "real-shopify-token"
    assert source1 == "client_credentials"
    # Second call within the cache window must not re-request -- exactly one real HTTP call.
    assert call_count == 1
    assert token2 == "real-shopify-token"
    assert source2 == "client_credentials_cached"

    # Never logs the actual token/secret.
    for record in caplog.records:
        assert "real-shopify-token" not in record.message
        assert "test-client-secret" not in record.message
    assert any("SHOPIFY_ADMIN_AUTH_OK" in r.message for r in caplog.records)
    assert any("SHOPIFY_AUTH_SOURCE" in r.message for r in caplog.records)


async def test_client_credentials_401_falls_back_to_session_table(monkeypatch, caplog):
    monkeypatch.setattr(settings, "shopify_api_key", "test-client-id")
    monkeypatch.setattr(settings, "shopify_api_secret", "test-client-secret")

    async def _fake_401(self, url, json=None, **kwargs):
        request = httpx.Request("POST", url)
        return httpx.Response(401, request=request, text='{"errors":"[API] Invalid API key or access token"}')

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_401)

    async def _session_token(*_a, **_kw):
        return "fallback-session-token"

    monkeypatch.setattr("app.shopify.sessions.get_offline_access_token", _session_token)

    with caplog.at_level(logging.INFO, logger="app.shopify.admin_auth"):
        token, source = await admin_auth.get_admin_access_token(session=None, shop=SHOP)

    assert token == "fallback-session-token"
    assert source == "session_table"
    failed_records = [r for r in caplog.records if "SHOPIFY_ADMIN_AUTH_FAILED" in r.message]
    assert failed_records
    assert '"status": 401' in failed_records[0].message
    for record in caplog.records:
        assert "test-client-secret" not in record.message


async def test_client_credentials_response_json_never_logged_verbatim(monkeypatch, caplog):
    # Defense in depth: even if a future response body carried something sensitive, the log line
    # for a failure only ever includes shop/method/reason/status -- never the raw response body.
    monkeypatch.setattr(settings, "shopify_api_key", "test-client-id")
    monkeypatch.setattr(settings, "shopify_api_secret", "test-client-secret")

    async def _fake_500(self, url, json=None, **kwargs):
        request = httpx.Request("POST", url)
        return httpx.Response(500, request=request, text="super-secret-internal-detail")

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_500)

    async def _no_session_token(*_a, **_kw):
        return None

    monkeypatch.setattr("app.shopify.sessions.get_offline_access_token", _no_session_token)

    with caplog.at_level(logging.INFO, logger="app.shopify.admin_auth"):
        token, source = await admin_auth.get_admin_access_token(session=None, shop=SHOP)

    assert token is None
    assert source == "none"
    for record in caplog.records:
        assert "super-secret-internal-detail" not in record.message
