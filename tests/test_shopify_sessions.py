import uuid

from sqlalchemy import delete

from app.db.models import Session as ShopifySession
from app.shopify.sessions import DEFAULT_SHOP_DOMAIN, get_offline_access_token, resolve_shop_domain


def _shop(label: str) -> str:
    return f"pytest-{label}-{uuid.uuid4().hex[:8]}.myshopify.com"


async def test_returns_offline_token_for_matching_shop(db_session):
    shop = _shop("offline")
    row = ShopifySession(id=uuid.uuid4().hex, shop=shop, state="x", isOnline=False, accessToken="shpat_offline_token")
    db_session.add(row)
    await db_session.commit()
    try:
        assert await get_offline_access_token(db_session, shop) == "shpat_offline_token"
    finally:
        await db_session.delete(row)
        await db_session.commit()


async def test_ignores_online_session_for_the_same_shop(db_session):
    shop = _shop("online-only")
    row = ShopifySession(id=uuid.uuid4().hex, shop=shop, state="x", isOnline=True, accessToken="shpat_online_token")
    db_session.add(row)
    await db_session.commit()
    try:
        assert await get_offline_access_token(db_session, shop) is None
    finally:
        await db_session.delete(row)
        await db_session.commit()


async def test_returns_none_for_unknown_shop(db_session):
    assert await get_offline_access_token(db_session, _shop("never-installed")) is None


async def test_resolve_shop_domain_returns_the_most_recent_offline_session(db_session):
    # "zzz..." sorts after any real cuid-style id (which starts with a lowercase letter well
    # before 'z'), so this deterministically becomes the row `ORDER BY id DESC` picks first --
    # without needing the shared table to be empty, which it never reliably is in this DB.
    shop = _shop("resolve-domain")
    row = ShopifySession(id=f"zzzzzzzz-{uuid.uuid4().hex}", shop=shop, state="x", isOnline=False, accessToken="shpat_x")
    db_session.add(row)
    await db_session.commit()
    try:
        assert await resolve_shop_domain(db_session) == shop
    finally:
        await db_session.execute(delete(ShopifySession).where(ShopifySession.id == row.id))
        await db_session.commit()


async def test_resolve_shop_domain_falls_back_to_default_when_no_offline_session_exists():
    # A fake connection that always reports "no row" -- proves the fallback branch itself works
    # without depending on the shared table actually being empty, which it never reliably is.
    class _EmptySession:
        async def scalar(self, *a, **kw):
            return None

    assert await resolve_shop_domain(_EmptySession()) == DEFAULT_SHOP_DOMAIN
