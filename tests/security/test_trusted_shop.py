"""Phase 1 regression tests for F1: the Shopify shop hostname is a single canonical trust
boundary. Invalid or untrusted shops must be rejected BEFORE any HTTP request, database lookup,
or cache read -- never "sent and rejected by Shopify". No real credentials appear here; the
sentinel values below are unmistakably fake."""

import httpx
import pytest

from app.config import settings
from app.shopify import admin_auth, admin_client
from app.shopify.admin_client import admin_graphql
from app.shopify.trusted_shop import (
    TrustedShopNotConfigured,
    UntrustedShopError,
    canonicalize_shop_hostname,
    is_trusted_shop,
    require_trusted_shop,
    trusted_shop,
)

TRUSTED = "trusted-store.myshopify.com"
FAKE_KEY = "fake-client-id-for-tests"
FAKE_SECRET = "fake-client-secret-for-tests"

REJECTED_SHOPS = [
    "attacker.com",
    "attacker.example.com",
    "evil.attacker.com",
    "localhost",
    "127.0.0.1",
    "10.0.0.5",
    "192.168.1.1",
    "169.254.169.254",
    "[::1]",
    "::1",
    "2001:db8::1",
    "trusted-store.myshopify.com.attacker.com",
    "attacker.com.trusted-store.myshopify.com",
    "sub.trusted-store.myshopify.com",
    "attacker@trusted-store.myshopify.com",
    "user:pass@trusted-store.myshopify.com",
    "trusted-store.myshopify.com:443",
    "trusted-store.myshopify.com:8443",
    "https://trusted-store.myshopify.com",
    "http://trusted-store.myshopify.com",
    "trusted-store.myshopify.com/admin",
    "trusted-store.myshopify.com/",
    "trusted-store.myshopify.com?x=1",
    "trusted-store.myshopify.com#frag",
    "trusted-store.myshopify.com.",
    ".trusted-store.myshopify.com",
    " trusted-store.myshopify.com",
    "trusted-store.myshopify.com ",
    "trusted-store.myshopify.com\n",
    "trusted-store.myshopify.com\x00",
    "trusted-store.myshopify.co",
    "trusted-store.myshopify.com.evil",
    "trusted-store.myshopifyXcom",
    "trusted-store.example.com",
    "other-store.myshopify.com",
    "trusted-store-2.myshopify.com",
    "trustedstore.myshopify.com",
    "-trusted-store.myshopify.com",
    "trusted-store-.myshopify.com",
    "truѕted-store.myshopify.com",  # Cyrillic 's' homoglyph
    "trusted‐store.myshopify.com",   # Unicode hyphen
    "ｔrusted-store.myshopify.com",   # fullwidth 't'
    "myshopify.com",
    ".myshopify.com",
    "",
    "not-a-url-at-all",
]


@pytest.fixture(autouse=True)
def _configure(monkeypatch):
    monkeypatch.setattr(settings, "shopify_shop_domain", TRUSTED)
    monkeypatch.setattr(settings, "shopify_api_key", FAKE_KEY)
    monkeypatch.setattr(settings, "shopify_api_secret", FAKE_SECRET)
    admin_auth._token_cache.clear()
    yield
    admin_auth._token_cache.clear()


@pytest.fixture
def forbid_http(monkeypatch):
    """Any HTTP attempt fails the test loudly."""
    calls = []

    def _boom(self, *a, **kw):
        calls.append((a, kw))
        raise AssertionError("HTTP request attempted for an untrusted shop")

    monkeypatch.setattr(httpx.AsyncClient, "post", _boom)
    monkeypatch.setattr(httpx.AsyncClient, "get", _boom)
    monkeypatch.setattr(httpx.AsyncClient, "request", _boom)
    return calls


@pytest.fixture
def forbid_session_lookup(monkeypatch):
    async def _boom(*a, **kw):
        raise AssertionError("Session table consulted for an untrusted shop")

    monkeypatch.setattr("app.shopify.sessions.get_offline_access_token", _boom)


# ---------------------------------------------------------------------------
# Canonicalization / allowlist
# ---------------------------------------------------------------------------

def test_exact_configured_shop_is_accepted():
    assert require_trusted_shop(TRUSTED) == TRUSTED
    assert trusted_shop() == TRUSTED
    assert is_trusted_shop(TRUSTED) is True


def test_case_is_canonicalized_to_the_configured_shop():
    assert require_trusted_shop("Trusted-Store.MyShopify.COM") == TRUSTED


@pytest.mark.parametrize("candidate", REJECTED_SHOPS, ids=[repr(s) for s in REJECTED_SHOPS])
def test_untrusted_or_malformed_shops_are_rejected(candidate):
    with pytest.raises(UntrustedShopError):
        require_trusted_shop(candidate)
    assert is_trusted_shop(candidate) is False


@pytest.mark.parametrize("candidate", [None, 123, b"trusted-store.myshopify.com", ["trusted-store.myshopify.com"], {"shop": TRUSTED}])
def test_non_string_shops_are_rejected(candidate):
    with pytest.raises(UntrustedShopError):
        require_trusted_shop(candidate)


def test_a_valid_but_different_myshopify_hostname_is_still_rejected():
    # Syntactically perfect; exact allowlist is what matters for a single-store app.
    assert canonicalize_shop_hostname("other-store.myshopify.com") == "other-store.myshopify.com"
    with pytest.raises(UntrustedShopError):
        require_trusted_shop("other-store.myshopify.com")


def test_unconfigured_trusted_shop_fails_closed_even_for_a_valid_hostname(monkeypatch):
    monkeypatch.setattr(settings, "shopify_shop_domain", "")
    with pytest.raises(TrustedShopNotConfigured):
        require_trusted_shop(TRUSTED)
    with pytest.raises(TrustedShopNotConfigured):
        trusted_shop()


def test_misconfigured_trusted_shop_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "shopify_shop_domain", "https://trusted-store.myshopify.com")
    with pytest.raises(TrustedShopNotConfigured):
        trusted_shop()


def test_error_messages_never_echo_the_candidate_value():
    for candidate in ("attacker.example.com", "https://evil.test/x"):
        with pytest.raises(UntrustedShopError) as info:
            require_trusted_shop(candidate)
        assert candidate not in str(info.value)


# ---------------------------------------------------------------------------
# Credential-bearing clients fail closed BEFORE any HTTP / DB / cache activity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("candidate", REJECTED_SHOPS[:24], ids=[repr(s) for s in REJECTED_SHOPS[:24]])
async def test_client_credentials_grant_never_leaves_for_an_untrusted_shop(candidate, forbid_http, forbid_session_lookup):
    with pytest.raises(UntrustedShopError):
        await admin_auth._request_client_credentials_token(candidate)
    with pytest.raises(UntrustedShopError):
        await admin_auth.get_admin_access_token(session=None, shop=candidate)
    assert forbid_http == []
    assert admin_auth._token_cache == {}


@pytest.mark.parametrize("candidate", REJECTED_SHOPS[:24], ids=[repr(s) for s in REJECTED_SHOPS[:24]])
async def test_admin_graphql_never_sends_a_token_to_an_untrusted_shop(candidate, forbid_http, forbid_session_lookup, monkeypatch):
    async def _never(*a, **kw):
        raise AssertionError("token lookup attempted for an untrusted shop")

    monkeypatch.setattr(admin_client, "get_admin_access_token", _never)
    with pytest.raises(UntrustedShopError):
        await admin_graphql(None, candidate, "query { shop { name } }")
    assert forbid_http == []


async def test_cached_token_is_not_served_for_an_untrusted_shop(forbid_http, forbid_session_lookup):
    # Even a poisoned cache entry keyed by an attacker host must not be honoured.
    admin_auth._token_cache["attacker.com"] = ("leaked-token", 10**12)
    with pytest.raises(UntrustedShopError):
        await admin_auth.get_admin_access_token(session=None, shop="attacker.com")
    assert forbid_http == []


async def test_trusted_shop_grant_goes_to_the_trusted_host_only_and_never_follows_redirects(monkeypatch):
    captured = []

    async def _fake_post(self, url, json=None, **kwargs):
        captured.append({"url": str(url), "json": json, "follow_redirects": self.follow_redirects})
        return httpx.Response(200, request=httpx.Request("POST", url), json={"access_token": "fake-admin-token", "expires_in": 3600})

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    token, source = await admin_auth.get_admin_access_token(session=None, shop="TRUSTED-STORE.myshopify.com")
    assert token == "fake-admin-token" and source == "client_credentials"
    assert captured == [{
        "url": f"https://{TRUSTED}/admin/oauth/access_token",
        "json": {"client_id": FAKE_KEY, "client_secret": FAKE_SECRET, "grant_type": "client_credentials"},
        "follow_redirects": False,
    }]


async def test_admin_graphql_posts_only_to_the_trusted_host_without_following_redirects(monkeypatch):
    captured = []

    async def _fake_token(session, shop):
        return "fake-admin-token", "client_credentials"

    async def _fake_post(self, url, json=None, headers=None, **kwargs):
        captured.append({"url": str(url), "headers": headers, "follow_redirects": self.follow_redirects})
        return httpx.Response(200, request=httpx.Request("POST", url), json={"data": {"shop": {"name": "x"}}})

    monkeypatch.setattr(admin_client, "get_admin_access_token", _fake_token)
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    result = await admin_graphql(None, TRUSTED, "query { shop { name } }")
    assert result == {"data": {"shop": {"name": "x"}}}
    assert captured[0]["url"] == f"https://{TRUSTED}/admin/api/{settings.shopify_api_version}/graphql.json"
    assert captured[0]["headers"]["X-Shopify-Access-Token"] == "fake-admin-token"
    assert captured[0]["follow_redirects"] is False


async def test_session_table_fallback_is_restricted_to_the_trusted_shop(monkeypatch):
    from app.shopify.sessions import get_offline_access_token

    class _NeverQueried:
        async def scalar(self, *a, **kw):
            raise AssertionError("Session table queried for an untrusted shop")

    with pytest.raises(UntrustedShopError):
        await get_offline_access_token(_NeverQueried(), "attacker.com")
