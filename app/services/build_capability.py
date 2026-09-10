"""Build capability tokens: the minimal ownership mechanism for Shopify build mutations
(Phase 1, findings F2 / N3).

Knowing a recommendation id, a conversation id, a product GID, or an Origin header is NOT
authorization. Instead, when the backend itself decides a recommendation is ready for the
customer (the preview_ready event), it mints a random 256-bit capability token, stores only its
SHA-256 hash, and hands the token to that customer's browser inside the preview URL. Every
preview read and every Shopify mutation (recreate / save_build / add_to_cart / slider re-price)
must present a token whose hash matches a live, unexpired, unrevoked row for THAT recommendation.

Properties:
  * server generated, `secrets.token_urlsafe(32)` (256 bits of entropy);
  * not derived from the recommendation id, conversation id, email, or name;
  * scoped to exactly one recommendation (and recorded against its conversation and shop);
  * expires (BUILD_TOKEN_TTL) and can be revoked;
  * only the hash is persisted; the plaintext is never logged (see preview_url_for_logging).

Storage is the BuildCapability table (see migrations/0001_build_capability.sql). If that table
does not exist yet, minting raises and the calling flow fails closed: no preview URL without a
capability, no mutation without a capability.
"""

import hashlib
import hmac
import secrets
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.ids import new_id
from app.db.models import BuildCapability
from app.db.time import utcnow

BUILD_TOKEN_TTL = timedelta(days=7)
BUILD_TOKEN_QUERY_PARAM = "bt"
_MAX_TOKEN_LENGTH = 128


class BuildNotAuthorized(Exception):
    """The presented capability does not authorize this recommendation. Customer-safe message."""

    def __init__(self, message: str = "This fragrance build link is not valid or has expired."):
        super().__init__(message)


def hash_build_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def issue_build_token(session: AsyncSession, *, recommendation_id: str, conversation_id: str, shop: str) -> str:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    session.add(
        BuildCapability(
            id=new_id(),
            recommendationId=recommendation_id,
            conversationId=conversation_id,
            shop=shop,
            tokenHash=hash_build_token(token),
            expiresAt=now + BUILD_TOKEN_TTL,
            revokedAt=None,
            createdAt=now,
        )
    )
    await session.commit()
    return token


async def authorize_build_token(session: AsyncSession, *, token: object, recommendation_id: object) -> BuildCapability:
    """Return the matching live capability or raise BuildNotAuthorized. Never raises anything that
    would reveal which check failed."""
    if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN_LENGTH:
        raise BuildNotAuthorized()
    if not isinstance(recommendation_id, str) or not recommendation_id:
        raise BuildNotAuthorized()
    row = await session.scalar(select(BuildCapability).where(BuildCapability.tokenHash == hash_build_token(token)))
    if row is None:
        raise BuildNotAuthorized()
    if not hmac.compare_digest(row.recommendationId.encode(), recommendation_id.encode()):
        raise BuildNotAuthorized()
    if row.revokedAt is not None:
        raise BuildNotAuthorized()
    if row.expiresAt <= utcnow():
        raise BuildNotAuthorized()
    return row


async def revoke_build_tokens(session: AsyncSession, recommendation_id: str) -> int:
    rows = (await session.execute(select(BuildCapability).where(BuildCapability.recommendationId == recommendation_id, BuildCapability.revokedAt.is_(None)))).scalars().all()
    now = utcnow()
    for row in rows:
        row.revokedAt = now
    await session.commit()
    return len(rows)


def preview_url_for_logging(url: str | None) -> str | None:
    """The same preview URL with the capability token removed, for log lines only."""
    if not url:
        return url
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != BUILD_TOKEN_QUERY_PARAM]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
