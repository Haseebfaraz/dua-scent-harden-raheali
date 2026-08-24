"""Reads the Session table Node's PrismaSessionStorage already writes to at install time --
Python never runs the OAuth flow itself, it only reads the offline access token that already
exists there. See app/db/models.Session and the migration audit for why this is enough.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as DbSession

from app.db.models import Session


async def get_offline_access_token(session: DbSession, shop: str) -> str | None:
    row = await session.scalar(select(Session).where(Session.shop == shop, Session.isOnline.is_(False)))
    return row.accessToken if row else None
