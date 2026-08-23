"""Prisma's DateTime maps to Postgres TIMESTAMP WITHOUT TIME ZONE by default (confirmed against
the real schema) -- every column here is tz-naive UTC, so Python must write naive UTC datetimes
too, or asyncpg raises a DataError mixing aware/naive values.
"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
