import uuid

from app.db.models import Session as ShopifySession
from app.shopify.sessions import get_offline_access_token


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
