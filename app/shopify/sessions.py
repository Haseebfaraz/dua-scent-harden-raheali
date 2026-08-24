"""Reads the Session table Node's PrismaSessionStorage already writes to at install time --
Python never runs the OAuth flow itself, it only reads the offline access token that already
exists there. See app/db/models.Session and the migration audit for why this is enough.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as DbSession

from app.db.models import Session

# Port of shopDomain.server.js's own hardcoded fallback -- used only if no offline session row
# exists at all (shouldn't happen once a merchant has installed).
DEFAULT_SHOP_DOMAIN = "test-3d-products.myshopify.com"


async def get_offline_access_token(session: DbSession, shop: str) -> str | None:
    row = await session.scalar(select(Session).where(Session.shop == shop, Session.isOnline.is_(False)))
    return row.accessToken if row else None


async def resolve_shop_domain(session: DbSession) -> str:
    """Port of shopDomain.server.js's resolveShopDomain -- this custom app is installed on
    exactly one real shop, whose Session row (from OAuth) is the actual source of truth; there is
    no incoming Shopify request to derive a shop from here (unlike App Proxy calls, which get a
    cryptographically verified shop straight from the signature check)."""
    row = await session.scalar(select(Session).where(Session.isOnline.is_(False)).order_by(Session.id.desc()))
    return row.shop if row else DEFAULT_SHOP_DOMAIN
