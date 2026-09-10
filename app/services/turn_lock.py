"""Per-conversation turn lock (Phase 2, finding F6 concurrency).

Exactly one model-bearing chat turn may run for a conversation at a time, across every service
instance, so five simultaneous requests cannot produce five OpenAI bills, racing profile writes,
or interleaved recommendations.

Implementation: a PostgreSQL session-level advisory lock held on a DEDICATED connection for the
duration of the turn. The request's ORM session cannot hold it because NullPool returns its
connection after every commit; a dedicated connection is held open until the context manager
exits (success or exception) and the lock is released explicitly and, as a backstop, by the
connection closing. Lock key: (classid 1, hashtext(conversation_id)).
"""

import contextlib
import logging
from collections.abc import AsyncIterator

from sqlalchemy import text

from app.db.session import engine

logger = logging.getLogger(__name__)

_LOCK_CLASS = 1


class TurnInProgress(Exception):
    def __init__(self, message: str = "A reply is already being written for this conversation. Please wait a moment and try again."):
        super().__init__(message)


@contextlib.asynccontextmanager
async def conversation_turn_lock(conversation_id: str) -> AsyncIterator[None]:
    """Acquire the per-conversation lock or raise TurnInProgress immediately (no waiting)."""
    connection = await engine.connect()
    acquired = False
    try:
        acquired = bool(await connection.scalar(text("SELECT pg_try_advisory_lock(:cls, hashtext(:cid))"), {"cls": _LOCK_CLASS, "cid": conversation_id}))
        if not acquired:
            raise TurnInProgress()
        yield
    finally:
        try:
            if acquired:
                await connection.execute(text("SELECT pg_advisory_unlock(:cls, hashtext(:cid))"), {"cls": _LOCK_CLASS, "cid": conversation_id})
        except Exception:  # noqa: BLE001 -- closing the connection releases the lock anyway
            logger.warning("TURN_LOCK_UNLOCK_FAILED")
        finally:
            await connection.close()
