import uuid

import pytest

from app.config import settings
from app.db.models import Session as ShopifySession
from app.shopify.sessions import get_offline_access_token, resolve_shop_domain
from app.shopify.trusted_shop import TrustedShopNotConfigured, UntrustedShopError

# The suite-wide trusted shop (tests/conftest.py). Phase 1 (F1): the Session table is only ever
# read FOR this shop, and resolve_shop_domain() no longer discovers a shop from the database or
# falls back to a hard-coded test store.
SHOP = "test-shop.myshopify.com"


async def test_returns_offline_token_for_the_trusted_shop(db_session):
    row = ShopifySession(id=uuid.uuid4().hex, shop=SHOP, state="x", isOnline=False, accessToken="fake-offline-token")
    db_session.add(row)
    await db_session.commit()
    try:
        assert await get_offline_access_token(db_session, SHOP) == "fake-offline-token"
    finally:
        await db_session.delete(row)
        await db_session.commit()


async def test_ignores_online_session_for_the_same_shop(db_session):
    row = ShopifySession(id=uuid.uuid4().hex, shop=SHOP, state="x", isOnline=True, accessToken="fake-online-token")
    db_session.add(row)
    await db_session.commit()
    try:
        assert await get_offline_access_token(db_session, SHOP) is None
    finally:
        await db_session.delete(row)
        await db_session.commit()


async def test_refuses_to_read_a_session_for_any_other_shop():
    class _NeverQueried:
        async def scalar(self, *a, **kw):
            raise AssertionError("Session table queried for an untrusted shop")

    with pytest.raises(UntrustedShopError):
        await get_offline_access_token(_NeverQueried(), f"pytest-{uuid.uuid4().hex[:8]}.myshopify.com")


async def test_resolve_shop_domain_is_the_configured_trusted_shop_not_a_database_row():
    class _NeverQueried:
        async def scalar(self, *a, **kw):
            raise AssertionError("resolve_shop_domain must not consult the database")

    assert await resolve_shop_domain(_NeverQueried()) == SHOP
    assert await resolve_shop_domain() == SHOP


async def test_resolve_shop_domain_has_no_hard_coded_fallback(monkeypatch):
    # The old DEFAULT_SHOP_DOMAIN ("test-3d-products.myshopify.com") fallback is gone: an
    # unconfigured deployment fails closed instead of silently talking to a test store.
    monkeypatch.setattr(settings, "shopify_shop_domain", "")
    with pytest.raises(TrustedShopNotConfigured):
        await resolve_shop_domain()
