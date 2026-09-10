"""PostgreSQL-backed fixed-window rate limiting (Phase 2, finding F6).

Why Postgres: the service runs one uvicorn process per Render instance and Render can add
instances, so a process-local counter is not a limiter. The app already has Postgres; a Redis
just for this would be a second stateful dependency. Cost per check: ONE statement (a
multi-row upsert with RETURNING) per request, never per streamed chunk.

Keys are `<class>:<subject>` where the subject is a keyed hash for network identities (a raw IP
is never stored) or the conversation id for per-conversation limits. Windows are fixed
(`window_start = floor(now / window) * window`); a request in a new window resets the count.
Counts are incremented even when the request is refused, so refused requests are not free.

Retention: rows older than two days (by updatedAt) are pruned opportunistically by
`prune_stale_buckets`, invoked by the limiter roughly once per 200 checks; a periodic job may
call it as well.

Failure policy: if the database is unavailable the limiter fails CLOSED (RateLimitUnavailable),
because nothing downstream (profile, history, capabilities) can work without it either.
"""

import hashlib
import hmac
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.time import utcnow

logger = logging.getLogger(__name__)

_PRUNE_PROBABILITY = 1 / 200
_PRUNE_AFTER = timedelta(days=2)


class RateLimited(Exception):
    def __init__(self, limit_class: str, retry_after_seconds: int):
        super().__init__(f"rate limited: {limit_class}")
        self.limit_class = limit_class
        self.retry_after_seconds = max(1, int(retry_after_seconds))


class RateLimitUnavailable(Exception):
    """The limiter's store could not be reached. Callers fail closed (503)."""


@dataclass(frozen=True)
class Limit:
    limit_class: str
    subject: str
    max_count: int
    window_seconds: int


def parse_limit(spec: str) -> tuple[int, int]:
    """'12/60' -> (12, 60). Invalid specs raise at startup rather than silently disabling."""
    count, window = spec.split("/")
    max_count, window_seconds = int(count), int(window)
    if max_count < 1 or window_seconds < 1:
        raise ValueError(f"invalid rate limit spec {spec!r}")
    return max_count, window_seconds


def hash_abuse_identity(value: str) -> str:
    """Keyed hash for network identities so buckets never hold a raw IP."""
    key = (settings.abuse_identity_hash_key or settings.customer_key_hash_salt or "").encode()
    if key:
        return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()[:32]
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def limit(limit_class: str, subject: str, spec: str) -> Limit:
    max_count, window_seconds = parse_limit(spec)
    return Limit(limit_class, subject, max_count, window_seconds)


def _window_start(now: datetime, window_seconds: int) -> datetime:
    epoch = int(now.replace(tzinfo=timezone.utc).timestamp())
    return datetime.fromtimestamp(epoch - (epoch % window_seconds), tz=timezone.utc).replace(tzinfo=None)


_UPSERT = text(
    'INSERT INTO "RateLimitBucket" ("key", "windowStart", "count", "updatedAt") '
    'VALUES (:key, :window_start, 1, :now) '
    'ON CONFLICT ("key") DO UPDATE SET '
    '"count" = CASE WHEN "RateLimitBucket"."windowStart" = EXCLUDED."windowStart" THEN "RateLimitBucket"."count" + 1 ELSE 1 END, '
    '"windowStart" = EXCLUDED."windowStart", "updatedAt" = EXCLUDED."updatedAt" '
    'RETURNING "count"'
)


async def enforce(session: AsyncSession, limits: list[Limit]) -> None:
    """Count this request against every limit and raise RateLimited for the first one exceeded.
    Raises RateLimitUnavailable if the store cannot be reached."""
    if not limits:
        return
    now = utcnow()
    try:
        results: list[tuple[Limit, int, datetime]] = []
        for lim in limits:
            window_start = _window_start(now, lim.window_seconds)
            count = await session.scalar(_UPSERT, {"key": f"{lim.limit_class}:{lim.subject}", "window_start": window_start, "now": now})
            results.append((lim, int(count or 0), window_start))
        await session.commit()
    except Exception as err:  # noqa: BLE001 -- any store failure is "unavailable", fail closed
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.error("RATE_LIMIT_STORE_UNAVAILABLE %s", type(err).__name__)
        raise RateLimitUnavailable() from err

    for lim, count, window_start in results:
        if count > lim.max_count:
            retry_after = (window_start + timedelta(seconds=lim.window_seconds) - now).total_seconds()
            logger.info("RATE_LIMITED %s", {"limitClass": lim.limit_class, "count": count, "max": lim.max_count})
            raise RateLimited(lim.limit_class, retry_after)

    if random.random() < _PRUNE_PROBABILITY:
        try:
            await prune_stale_buckets(session)
        except Exception:  # noqa: BLE001 -- pruning is best effort
            await session.rollback()


async def prune_stale_buckets(session: AsyncSession) -> int:
    result = await session.execute(text('DELETE FROM "RateLimitBucket" WHERE "updatedAt" < :cutoff'), {"cutoff": utcnow() - _PRUNE_AFTER})
    await session.commit()
    return result.rowcount or 0
