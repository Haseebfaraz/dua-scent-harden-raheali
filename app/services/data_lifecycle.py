"""Customer data lifecycle (Phase 6, corrected in Phase 6A; finding F11): ownership-aware
deletion, minimization of the operational records that must remain, resurrection protection, and
bounded retention.

This is engineering, not a legal-compliance claim. Deleting rows here does not delete copies held
by Shopify, logs, backups or the other application that shares this database
(docs/DATA_RETENTION_AND_DELETION.md sections 8 and 9).

OWNERSHIP. Everything a guest creates hangs off ONE conversation id. Deletion is always scoped to
one conversation and the records keyed to it. There is no deletion by email, by name or by shop,
and an id alone authorizes nothing (the route authorizes with the conversation capability first).

CONTRACT (Phase 6A). Deletion is SYNCHRONOUS and ATOMIC. Either everything promised locally is
committed in ONE transaction and the caller gets "deleted", or nothing at all is written (no
marker, no revocation) and the caller gets a retryable CONFLICT while an operation on the
conversation is in flight. There is no accepted-but-pending state, so completion never depends on
a request that has ended, a background task, a `finally` block, the customer retrying, or an
unscheduled maintenance command. Phase 6 had such a state (a `deleting` tombstone finished by the
in-flight operation, a retry, or age-based retention); it was removed because its completion path
was gated by the disabled retention flag.

WHAT IS REMOVED for a conversation:
    Message (+ MessageSecurityClassification by the declared cascade), CustomerProfileState,
    ConversationCapability, BuildCapability, the conversation's RateLimitBucket rows, the
    Conversation row, and every FragranceRecommendation that has no commerce footprint (+ its
    inventory snapshot by the declared cascade).

WHAT IS KEPT, and why:
  * a FragranceRecommendation with a commerce footprint (a Shopify product id, or build status
    creating / pending_review / saved): a Shopify product exists, or may exist, whose metafield
    names this recommendation id; the row is what reconciliation, pending-review recovery and
    duplicate-creation prevention work from. It is reduced to an explicit ALLOWLIST of fields
    (_RETAINED_RECOMMENDATION_FIELDS); everything else is blanked; its conversation id becomes a
    random DETACHED value; and no further customer write is ever accepted for it. MINIMIZED, not
    anonymous: the Shopify product still carries customer data (finding N7).
  * CustomerAccountUrls (Phase 6A): written by the other application, holds no personal data
    (endpoint URLs), ownership unverified -> left untouched.

TWO LOCKING RULES (docs section 6):
  W (writers): every write of conversation-owned data runs inside a transaction that first takes
     the SHARED write-guard lock for that conversation (pg_advisory_xact_lock_shared, class 4)
     and then checks that no tombstone exists (ensure_conversation_writable), and commits before
     any external call. A separate transaction means a separate guard call.
  D (deletion): try the conversation turn lock (class 1) and every build lock (class 2, by id),
     then in ONE transaction try the EXCLUSIVE write-guard lock (class 4), insert the completed
     tombstone, and purge. Any lock that cannot be taken -> CONFLICT, nothing written.
  Because a writer holds the shared lock from its check to its commit, and deletion needs the
  exclusive lock for its whole transaction, a writer can never commit old data after the
  tombstone exists: it either committed before deletion began, in which case its rows are purged,
  or it takes its lock after deletion committed, in which case it sees the tombstone. This holds
  for direct service callers and other instances alike; it is a database guarantee, not a cache
  one. The turn and build locks add conflict semantics so an in-flight operation finishes cleanly.

All deletion-side locks are try-locks in a fixed order (1, then 2 by id, then 4), so nothing
waits and nothing can deadlock. Writers' shared lock requests do wait, but only for the
milliseconds of a purge transaction.
"""

import hashlib
import json
import logging
import secrets
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import (
    BuildCapability, Conversation, ConversationCapability, ConversationDeletion, CustomerProfileState, FragranceRecommendation, Message,
)
from app.db.time import utcnow

logger = logging.getLogger(__name__)

ORIGIN_CUSTOMER = "customer"
ORIGIN_RETENTION = "retention"
STATE_COMPLETED = "completed"
STATE_CONFLICT = "conflict"

_WRITE_GUARD_LOCK_CLASS = 4  # 1 = conversation turn lock, 2 = build lock, 3 = retention run

# Build states that mean a Shopify object exists or may exist.
_COMMERCE_BUILD_STATES = ("creating", "pending_review", "saved")
# Build states that are never resolved automatically: an operator must reconcile them first.
UNRESOLVED_BUILD_STATES = ("creating", "pending_review")
_CONVERSATION_LIMIT_CLASSES = ("chat_turn_conv", "chat_turn_conv_day", "history_read_conv", "security_denied")

# The ONLY columns of a retained commerce record that keep their value. Everything else in the
# row is blanked by minimize_recommendation(). An allowlist, deliberately: a new column is dropped
# by default instead of being kept by accident.
_RETAINED_RECOMMENDATION_FIELDS = frozenset({
    "id", "productsJson", "ratiosJson", "combinationType", "status", "buildStatus", "shopifyProductId", "shopifyVariantId",
    "createdAt", "confirmedAt",
})
_BLANK_VALUES: dict[str, Any] = {
    "customerProfileJson": {}, "scoreJson": {}, "evidenceJson": {}, "evidenceScope": None, "customerFacingJson": None,
    "draftName": None, "draftExcludedNotes": None, "draftRatiosJson": None,
}
DETACHED_PREFIX = "deleted-"


class ConversationDeleted(Exception):
    """A write was attempted for a conversation whose deletion has completed (or for a
    minimized, detached commerce record)."""


def _now() -> datetime:
    return utcnow()


def conversation_key(conversation_id: str) -> str:
    return hashlib.sha256(f"conversation:{conversation_id}".encode()).hexdigest()


def is_detached(conversation_id: str | None) -> bool:
    return isinstance(conversation_id, str) and conversation_id.startswith(DETACHED_PREFIX)


# ---------------------------------------------------------------------------
# Rule W: the write guard
# ---------------------------------------------------------------------------

async def is_conversation_deleted(session: AsyncSession, conversation_id: str | None) -> bool:
    if not conversation_id or not isinstance(conversation_id, str):
        return False
    return (await session.scalar(select(ConversationDeletion.conversationKey).where(ConversationDeletion.conversationKey == conversation_key(conversation_id)))) is not None


async def ensure_conversation_writable(session: AsyncSession, conversation_id: str | None) -> None:
    """Called by EVERY write path that could create or re-create conversation-owned data, at the
    start of the transaction that performs the write. Takes the shared write-guard lock for the
    rest of that transaction, then checks the tombstone."""
    if not conversation_id or not isinstance(conversation_id, str):
        return
    if is_detached(conversation_id):
        raise ConversationDeleted()  # a minimized commerce record accepts no further customer write
    await session.execute(text("SELECT pg_advisory_xact_lock_shared(:cls, hashtext(:cid))"), {"cls": _WRITE_GUARD_LOCK_CLASS, "cid": conversation_id})
    if await is_conversation_deleted(session, conversation_id):
        logger.warning("DATA_LIFECYCLE_WRITE_REFUSED %s", json.dumps({"reason": "conversation_deleted"}))
        raise ConversationDeleted()


def evict_local_caches(conversation_id: str) -> None:
    """Process-local only. Rule W is what protects the other instances; this just stops THIS
    process holding the content in memory."""
    from app.ai import conversation_flow, tool_executor

    conversation_flow._CONVERSATIONS.pop(conversation_id, None)
    tool_executor._conversation_scratch.pop(conversation_id, None)


# ---------------------------------------------------------------------------
# Rule D: deletion
# ---------------------------------------------------------------------------

@dataclass
class DeletionResult:
    state: str                 # completed | conflict
    removed: dict[str, int] = field(default_factory=dict)
    minimized_commerce_records: int = 0
    unresolved_commerce_records: int = 0
    already_deleted: bool = False

    @property
    def completed(self) -> bool:
        return self.state == STATE_COMPLETED


async def delete_conversation(session: AsyncSession, conversation_id: str, *, origin: str = ORIGIN_CUSTOMER) -> DeletionResult:
    """Synchronous, atomic, idempotent. Returns `completed` only after the single transaction that
    removed everything promised has committed; returns `conflict` (nothing written) while a chat
    turn, a build operation or a writer's transaction is in flight."""
    from app.services.build_commerce import BuildOperationInProgress, build_commerce_lock
    from app.services.turn_lock import TurnInProgress, conversation_turn_lock

    key = conversation_key(conversation_id)
    if await is_conversation_deleted(session, conversation_id):
        held = await session.scalar(select(ConversationDeletion.heldRecords).where(ConversationDeletion.conversationKey == key)) or 0
        await session.commit()
        return DeletionResult(state=STATE_COMPLETED, minimized_commerce_records=held, already_deleted=True)
    recommendation_ids = sorted((await session.execute(select(FragranceRecommendation.id).where(FragranceRecommendation.conversationId == conversation_id))).scalars())
    await session.commit()  # no transaction is held open while the session-level locks are taken

    async with AsyncExitStack() as stack:
        try:
            await stack.enter_async_context(conversation_turn_lock(conversation_id))      # class 1: the conversation
            for recommendation_id in recommendation_ids:                                  # class 2: its builds, by id
                await stack.enter_async_context(build_commerce_lock(recommendation_id))
        except (TurnInProgress, BuildOperationInProgress):
            logger.info("DATA_LIFECYCLE_DELETION_CONFLICT %s", json.dumps({"reason": "operation_in_flight"}))
            return DeletionResult(state=STATE_CONFLICT)

        try:
            # ---- ONE transaction from here to the commit ----
            exclusive = await session.scalar(text("SELECT pg_try_advisory_xact_lock(:cls, hashtext(:cid))"), {"cls": _WRITE_GUARD_LOCK_CLASS, "cid": conversation_id})
            if not exclusive:
                await session.rollback()  # a writer is inside its transaction: nothing written
                logger.info("DATA_LIFECYCLE_DELETION_CONFLICT %s", json.dumps({"reason": "writer_in_transaction"}))
                return DeletionResult(state=STATE_CONFLICT)
            now = _now()
            session.add(ConversationDeletion(conversationKey=key, origin=origin, completedAt=now, expiresAt=now + timedelta(days=settings.retention_tombstone_days), heldRecords=0))
            await session.flush()  # the tombstone row exists inside the transaction before the purge
            result = await _purge(session, conversation_id)
            tombstone = await session.scalar(select(ConversationDeletion).where(ConversationDeletion.conversationKey == key))
            tombstone.heldRecords = result.minimized_commerce_records
            await session.commit()
        except Exception:
            await session.rollback()  # atomic: a failure leaves no marker, no revocation, no partial purge
            raise
    evict_local_caches(conversation_id)
    logger.info("DATA_LIFECYCLE_DELETION_COMPLETED %s", json.dumps({"origin": origin, "removed": result.removed, "minimized": result.minimized_commerce_records, "unresolved": result.unresolved_commerce_records}))
    return result


def _has_commerce_footprint(recommendation: FragranceRecommendation) -> bool:
    return bool(recommendation.shopifyProductId) or recommendation.buildStatus in _COMMERCE_BUILD_STATES


def minimize_recommendation(recommendation: FragranceRecommendation) -> None:
    """Reduce a retained commerce record to the allowlist. Every mapped column that is not on the
    allowlist must have an explicit blank value here; an unknown column fails loudly rather than
    being kept silently."""
    for column in FragranceRecommendation.__table__.columns.keys():
        if column in _RETAINED_RECOMMENDATION_FIELDS:
            continue
        if column == "conversationId":
            recommendation.conversationId = DETACHED_PREFIX + secrets.token_hex(16)  # random: not derivable from the old id
            continue
        if column not in _BLANK_VALUES:
            raise RuntimeError(f"no minimization rule for FragranceRecommendation.{column}")
        setattr(recommendation, column, _BLANK_VALUES[column])
    products = recommendation.productsJson if isinstance(recommendation.productsJson, list) else []
    recommendation.productsJson = [{"title": p.get("title"), "notes": p.get("notes")} for p in products if isinstance(p, dict)]


async def _purge(session: AsyncSession, conversation_id: str) -> DeletionResult:
    """Runs inside the deletion transaction and locks. Explicit deletes only: nothing here relies
    on, or changes, a cascade beyond the ones the shared schema already declares."""
    removed: dict[str, int] = {}
    minimized = unresolved = 0

    recommendations = list((await session.execute(select(FragranceRecommendation).where(FragranceRecommendation.conversationId == conversation_id))).scalars())
    disposable_ids = []
    for recommendation in recommendations:
        if _has_commerce_footprint(recommendation):
            if recommendation.buildStatus in UNRESOLVED_BUILD_STATES:
                unresolved += 1
            minimize_recommendation(recommendation)
            minimized += 1
        else:
            disposable_ids.append(recommendation.id)

    async def _count(statement) -> int:
        return (await session.execute(statement)).rowcount or 0

    removed["buildCapabilities"] = await _count(delete(BuildCapability).where(BuildCapability.conversationId == conversation_id))
    if disposable_ids:
        removed["recommendations"] = await _count(delete(FragranceRecommendation).where(FragranceRecommendation.id.in_(disposable_ids)))
    removed["messages"] = await _count(delete(Message).where(Message.conversationId == conversation_id))
    removed["profiles"] = await _count(delete(CustomerProfileState).where(CustomerProfileState.conversationId == conversation_id))
    removed["conversationCapabilities"] = await _count(delete(ConversationCapability).where(ConversationCapability.conversationId == conversation_id))
    bucket_keys = [f"{limit_class}:{conversation_id}" for limit_class in _CONVERSATION_LIMIT_CLASSES]
    removed["rateLimitBuckets"] = await _count(text('DELETE FROM "RateLimitBucket" WHERE "key" = ANY(:keys)').bindparams(keys=bucket_keys))
    removed["conversations"] = await _count(delete(Conversation).where(Conversation.id == conversation_id))
    return DeletionResult(state=STATE_COMPLETED, removed={k: v for k, v in removed.items() if v}, minimized_commerce_records=minimized, unresolved_commerce_records=unresolved)


# ---------------------------------------------------------------------------
# Retention (scripts/data_retention.py; never scheduled or exposed over HTTP here)
# ---------------------------------------------------------------------------

@dataclass
class RetentionReport:
    dry_run: bool
    cutoffs: dict[str, str] = field(default_factory=dict)
    eligible: dict[str, int] = field(default_factory=dict)
    deleted: dict[str, int] = field(default_factory=dict)
    minimized: int = 0
    held: dict[str, int] = field(default_factory=dict)
    failed: int = 0
    batches: int = 0
    more_remaining: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Counts only. Never an id, a name, a message or any other customer content."""
        return {"dryRun": self.dry_run, "cutoffsUtc": self.cutoffs, "eligible": self.eligible, "deleted": self.deleted, "minimized": self.minimized,
                "held": self.held, "failed": self.failed, "batches": self.batches, "moreRemaining": self.more_remaining}


_RETENTION_LOCK_CLASS = 3
_INACTIVE_CONVERSATIONS_SQL = """
    SELECT c."id"
    FROM "Conversation" c
    WHERE COALESCE((SELECT MAX(m."createdAt") FROM "Message" m WHERE m."conversationId" = c."id" AND m."role" = 'user'), c."createdAt") < :cutoff
    ORDER BY c."createdAt", c."id"
    LIMIT :limit
"""


def _add(target: dict[str, int], key: str, amount: int) -> None:
    if amount:
        target[key] = target.get(key, 0) + amount


async def run_retention(session: AsyncSession, *, execute: bool = False, now: datetime | None = None, batch_size: int | None = None, max_batches: int = 50) -> RetentionReport:
    """AGE-BASED cleanup only; a customer's explicit deletion never depends on it. Dry-run unless
    `execute` is true AND settings.retention_execution_enabled is true. One run at a time across
    all workers (advisory lock class 3). Bounded: at most max_batches x batch_size conversations
    per run, each its own atomic deletion, so a failure never reports success and a retry simply
    continues."""
    now = now or _now()
    execute = bool(execute and settings.retention_execution_enabled)
    size = max(1, min(batch_size or settings.retention_batch_size, 1000))
    report = RetentionReport(dry_run=not execute)
    cut = {
        "inactiveConversation": now - timedelta(days=settings.retention_inactive_conversation_days),
        "deadCapability": now - timedelta(days=settings.retention_dead_capability_days),
        "commerceRecord": now - timedelta(days=settings.retention_commerce_record_days),
        "rateLimitBucket": now - timedelta(days=settings.retention_rate_limit_bucket_days),
    }
    report.cutoffs = {name: value.isoformat() + "Z" for name, value in cut.items()}

    from app.db.session import engine

    lock_connection = await engine.connect()
    try:
        if not bool(await lock_connection.scalar(text("SELECT pg_try_advisory_lock(:cls, 0)"), {"cls": _RETENTION_LOCK_CLASS})):
            report.held["anotherRetentionRunActive"] = 1
            return report
        try:
            await _retain(session, report, cut, now, execute, size, max_batches)
        finally:
            await lock_connection.execute(text("SELECT pg_advisory_unlock(:cls, 0)"), {"cls": _RETENTION_LOCK_CLASS})
    finally:
        await lock_connection.close()
    logger.info("DATA_LIFECYCLE_RETENTION_RUN %s", json.dumps(report.as_dict()))
    return report


async def _retain(session: AsyncSession, report: RetentionReport, cut: dict[str, datetime], now: datetime, execute: bool, size: int, max_batches: int) -> None:
    # 2. Inactive guest conversations. Stable order, bounded batches; a processed conversation
    #    disappears from the query, so there is no offset to drift.
    skipped: set[str] = set()
    while report.batches < max_batches:
        rows = [r for r in (await session.execute(text(_INACTIVE_CONVERSATIONS_SQL), {"cutoff": cut["inactiveConversation"], "limit": size + len(skipped)})).scalars() if r not in skipped]
        await session.commit()
        if not rows:
            break
        report.batches += 1
        _add(report.eligible, "inactiveConversations", len(rows))
        if not execute:
            report.more_remaining = len(rows) >= size
            break
        for conversation_id in rows[:size]:
            try:
                result = await delete_conversation(session, conversation_id, origin=ORIGIN_RETENTION)
            except Exception as err:  # noqa: BLE001
                await session.rollback()
                report.failed += 1
                skipped.add(conversation_id)
                logger.error("DATA_LIFECYCLE_RETENTION_ITEM_FAILED %s", json.dumps({"errorType": type(err).__name__}))
                continue
            if result.completed:
                for name, amount in result.removed.items():
                    _add(report.deleted, name, amount)
                report.minimized += result.minimized_commerce_records
            else:
                _add(report.held, "operationInFlight", 1)
                skipped.add(conversation_id)
    else:
        report.more_remaining = True

    # 4. Dead capabilities (hashes only).
    for model, name in ((ConversationCapability, "conversationCapabilities"), (BuildCapability, "buildCapabilities")):
        dead = (model.expiresAt < cut["deadCapability"]) | ((model.revokedAt.is_not(None)) & (model.revokedAt < cut["deadCapability"]))
        count = await session.scalar(select(text("count(*)")).select_from(model).where(dead)) or 0
        _add(report.eligible, name, count)
        if execute and count:
            ids = list((await session.execute(select(model.id).where(dead).order_by(model.id).limit(size * max_batches))).scalars())
            _add(report.deleted, name, (await session.execute(delete(model).where(model.id.in_(ids)))).rowcount or 0)
            await session.commit()

    # 5. Minimized commerce records past their period. Unresolved ones are HELD and reported.
    is_detached = FragranceRecommendation.conversationId.like(f"{DETACHED_PREFIX}%")
    detached = is_detached & (FragranceRecommendation.createdAt < cut["commerceRecord"])
    # HELD: every minimized record whose build is unresolved, whatever its age. Counted once per
    # run (a state, not an event) so an operator sees how many still need reconciliation.
    unresolved = await session.scalar(select(text("count(*)")).select_from(FragranceRecommendation).where(is_detached & FragranceRecommendation.buildStatus.in_(UNRESOLVED_BUILD_STATES))) or 0
    if unresolved:
        report.held["unresolvedCommerceRecords"] = unresolved
    releasable = detached & FragranceRecommendation.buildStatus.not_in(UNRESOLVED_BUILD_STATES)
    count = await session.scalar(select(text("count(*)")).select_from(FragranceRecommendation).where(releasable)) or 0
    _add(report.eligible, "commerceRecords", count)
    if execute and count:
        ids = list((await session.execute(select(FragranceRecommendation.id).where(releasable).order_by(FragranceRecommendation.id).limit(size * max_batches))).scalars())
        _add(report.deleted, "commerceRecords", (await session.execute(delete(FragranceRecommendation).where(FragranceRecommendation.id.in_(ids)))).rowcount or 0)
        await session.commit()

    # 6. Rate-limit buckets and expired tombstones (bounded lifecycle for the tombstones too).
    for statement_count, statement_delete, name, params in (
        ('SELECT count(*) FROM "RateLimitBucket" WHERE "updatedAt" < :cutoff', 'DELETE FROM "RateLimitBucket" WHERE "updatedAt" < :cutoff', "rateLimitBuckets", {"cutoff": cut["rateLimitBucket"]}),
        ('SELECT count(*) FROM "ConversationDeletion" WHERE "expiresAt" < :cutoff', 'DELETE FROM "ConversationDeletion" WHERE "expiresAt" < :cutoff', "tombstones", {"cutoff": now}),
    ):
        count = await session.scalar(text(statement_count), params) or 0
        _add(report.eligible, name, count)
        if execute and count:
            _add(report.deleted, name, (await session.execute(text(statement_delete), params)).rowcount or 0)
            await session.commit()
    await session.commit()
