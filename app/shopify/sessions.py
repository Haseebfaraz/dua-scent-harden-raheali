"""Reads the Session table Node's PrismaSessionStorage already writes to at install time --
Python never runs the OAuth flow itself, it only reads the offline access token that already
exists there. See app/db/models.Session and the migration audit for why this is enough.

Phase 1 (security, F1): the shop is no longer discovered from the database or defaulted to a
hard-coded test store. The one trusted shop comes from SHOPIFY_SHOP_DOMAIN (see
app/shopify/trusted_shop.py); the Session row is only ever read FOR that shop.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as DbSession

from app.db.models import Session
from app.shopify.trusted_shop import require_trusted_shop, trusted_shop


async def get_offline_access_token(session: DbSession, shop: str) -> str | None:
    shop = require_trusted_shop(shop)
    row = await session.scalar(select(Session).where(Session.shop == shop, Session.isOnline.is_(False)))
    return row.accessToken if row else None


async def resolve_shop_domain(session: DbSession | None = None) -> str:
    """The trusted configured shop. The `session` parameter is kept for call-site compatibility
    and is unused: the database is not a source of truth for which shop we talk to."""
    return trusted_shop()
