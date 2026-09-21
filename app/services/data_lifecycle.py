"""Customer data lifecycle (Phase 6, finding F11): ownership-aware deletion, minimization of the
operational records that must remain, resurrection protection, and bounded retention.

This is engineering, not a legal-compliance claim. Deleting rows here does not delete copies held
by Shopify, logs, backups or the other application that shares this database
(docs/DATA_RETENTION_AND_DELETION.md sections 8 and 9).

OWNERSHIP. Everything a guest creates hangs off ONE conversation id. Deletion is always scoped to
one conversation and the records keyed to it. There is no deletion by email, by name or by shop,
and an id alone authorizes nothing (the routes authorize with the conversation capability first).

WHAT IS REMOVED for a conversation:
    Message (+ MessageSecurityClassification by cascade), CustomerProfileState,
    CustomerAccountUrls, ConversationCapability, BuildCapability, the conversation's
    RateLimitBucket rows, the Conversation row, and every FragranceRecommendation that has no
    commerce footprint (+ its inventory snapshot by cascade).

WHAT IS KEPT, and why: a FragranceRecommendation with a commerce footprint (a Shopify product id,
or build status creating / pending_review / saved). A Shopify product exists, or may exist, whose
metafield names this recommendation id; the row is what reconciliation, pending-review recovery
and duplicate-creation prevention work from. It is reduced to an explicit ALLOWLIST of fields
(_RETAINED_RECOMMENDATION_FIELDS); everything else is blanked, and its conversation id is replaced
by a random value so it no longer points at the deleted conversation. This is called MINIMIZED,
not anonymous: the Shopify product it references still carries customer data (finding N7), and
the recommendation id still appears in earlier logs.

RESURRECTION. A durable tombstone (ConversationDeletion) is committed BEFORE anything is removed
and capabilities are revoked in the same transaction. Every write path that could recreate the
conversation's data calls ensure_conversation_writable() and refuses once a tombstone exists, on
every instance, whatever a process has cached. The tombstone holds a SHA-256 of the conversation
id and nothing else about the customer, and is itself removed after RETENTION_TOMBSTONE_DAYS.

LOCK ORDER (all try-locks, nothing ever waits, so no deadlock is possible): conversation turn lock
(class 1) first, then the build lock (class 2) of each recommendation in id order. If a chat turn
or a build operation is in flight, the purge does not happen now: the deletion stays `deleting`
(reported to the caller as pending, never as complete) and is finished by the in-flight operation
when it releases its lock, by a repeated request, or by the retention command.
"""

import hashlib
import json
import logging
import secrets
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import (
    BuildCapability, Conversation, ConversationCapability, ConversationDeletion, CustomerAccountUrls, CustomerProfileState,
    FragranceRecommendation, Message,
)
from app.db.time import utcnow

logger = logging.getLogger(__name__)

STATE_DELETING = "deleting"
STATE_COMPLETED = "completed"
ORIGIN_CUSTOMER = "customer"
ORIGIN_RETENTION = "retention"

# Build states that mean a Shopify object exists or may exist.
_COMMERCE_BUILD_STATES = ("creating", "pending_review", "saved")
# Build states that are never resolved automatically: an operator must reconcile them first.
UNRESOLVED_BUILD_STATES = ("creating", "pending_review")

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
_CONVERSATION_LIMIT_CLASSES = ("chat_turn_conv", "chat_turn_conv_day", "history_read_conv", "security_denied")


class ConversationDeleted(Exception):
    """A write was attempted for a conversation whose deletion has been requested or completed."""


def _now() -> datetime:
    return utcnow()


def conversation_key(conversation_id: str) -> str:
    return hashlib.sha256(f"conversation:{conversation_id}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Resurrection guard
# ---------------------------------------------------------------------------

async def is_conversation_deleted(session: AsyncSession, conversation_id: str | None) -> bool:
    if not conversation_id or not isinstance(conversation_id, str):
        return False
    return (await session.scalar(select(ConversationDeletion.conversationKey).where(ConversationDeletion.conversationKey == conversation_key(conversation_id)))) is not None


async def ensure_conversation_writable(session: AsyncSession, conversation_id: str | None) -> None:
    """Called by EVERY write path that could create or re-create conversation-owned data."""
    if await is_conversation_deleted(session, conversation_id):
        logger.warning("DATA_LIFECYCLE_WRITE_REFUSED %s", json.dumps({"reason": "conversation_deleted"}))
        raise ConversationDeleted()


def evict_local_caches(conversation_id: str) -> None:
    """Process-local only. The durable tombstone and the revoked capabilities are what protect
    the other instances; this just stops THIS process holding the content in memory."""
    from app.ai import conversation_flow, tool_executor

    conversation_flow._CONVERSATIONS.pop(conversation_id, None)
    tool_executor._conversation_scratch.pop(conversation_id, None)


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

@dataclass
class DeletionResult:
    state: str                 # completed | deleting (= pending)
    removed: dict[str, int] = field(default_factory=dict)
    minimized_commerce_records: int = 0
    unresolved_commerce_records: int = 0

    @property
    def completed(self) -> bool:
        return self.state == STATE_COMPLETED


async def request_conversation_deletion(session: AsyncSession, conversation_id: str, *, origin: str = ORIGIN_CUSTOMER) -> DeletionResult:
    """Step 1 (durable, immediate): tombstone + capability revocation in ONE transaction. From this
    commit on, every instance refuses the old capabilities and every guarded write.
    Step 2: purge, if nothing for this conversation is in flight. Idempotent."""
    now = _now()
    key = conversation_key(conversation_id)
    existing = await session.scalar(select(ConversationDeletion).where(ConversationDeletion.conversationKey == key))
    if existing is None:
        session.add(ConversationDeletion(conversationKey=key, pendingConversationId=conversation_id, state=STATE_DELETING, origin=origin, requestedAt=now, heldRecords=0))
        await session.execute(update(ConversationCapability).where(ConversationCapability.conversationId == conversation_id, ConversationCapability.revokedAt.is_(None)).values(revokedAt=now))
        await session.execute(update(BuildCapability).where(BuildCapability.conversationId == conversation_id, BuildCapability.revokedAt.is_(None)).values(revokedAt=now))
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    elif existing.state == STATE_COMPLETED:
        return DeletionResult(state=STATE_COMPLETED, minimized_commerce_records=existing.heldRecords)
    evict_local_caches(conversation_id)
    return await complete_conversation_deletion(session, conversation_id)


async def complete_pending_deletion(session: AsyncSession, conversation_id: str | None) -> None:
    """Called by a chat turn / build operation right after it releases its lock, so a deletion
    that had to wait for it is finished promptly. Never raises into the caller."""
    try:
        if conversation_id and await session.scalar(select(ConversationDeletion.state).where(ConversationDeletion.conversationKey == conversation_key(conversation_id))) == STATE_DELETING:
            await complete_conversation_deletion(session, conversation_id)
    except Exception as err:  # noqa: BLE001
        logger.error("DATA_LIFECYCLE_COMPLETION_FAILED %s", json.dumps({"errorType": type(err).__name__}))


async def complete_conversation_deletion(session: AsyncSession, conversation_id: str) -> DeletionResult:
    from app.services.build_commerce import BuildOperationInProgress, build_commerce_lock
    from app.services.turn_lock import TurnInProgress, conversation_turn_lock

    key = conversation_key(conversation_id)
    recommendation_ids = sorted((await session.execute(select(FragranceRecommendation.id).where(FragranceRecommendation.conversationId == conversation_id))).scalars())
    await session.commit()  # no transaction is held open while locks are taken

    async with AsyncExitStack() as stack:
        try:
            await stack.enter_async_context(conversation_turn_lock(conversation_id))      # 1st: the conversation
            for recommendation_id in recommendation_ids:                                  # 2nd: its builds, in id order
                await stack.enter_async_context(build_commerce_lock(recommendation_id))
        except (TurnInProgress, BuildOperationInProgress):
            logger.info("DATA_LIFECYCLE_DELETION_PENDING %s", json.dumps({"reason": "operation_in_flight"}))
            return DeletionResult(state=STATE_DELETING)

        try:
            result = await _purge(session, conversation_id)
            tombstone = await session.scalar(select(ConversationDeletion).where(ConversationDeletion.conversationKey == key))
            if tombstone is not None:
                now = _now()
                tombstone.state = STATE_COMPLETED
                tombstone.pendingConversationId = None  # from here on only the hash remains
                tombstone.completedAt = now
                tombstone.expiresAt = now + timedelta(days=settings.retention_tombstone_days)
                tombstone.heldRecords = result.minimized_commerce_records
            await session.commit()  # the purge and the state change are ONE transaction
        except Exception:
            await session.rollback()
            raise
    evict_local_caches(conversation_id)
    logger.info("DATA_LIFECYCLE_DELETION_COMPLETED %s", json.dumps({"removed": result.removed, "minimized": result.minimized_commerce_records, "unresolved": result.unresolved_commerce_records}))
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
    # productsJson is kept for the recipe, but only its recipe keys.
    products = recommendation.productsJson if isinstance(recommendation.productsJson, list) else []
    recommendation.productsJson = [{"title": p.get("title"), "notes": p.get("notes")} for p in products if isinstance(p, dict)]


async def _purge(session: AsyncSession, conversation_id: str) -> DeletionResult:
    """Runs inside the caller's transaction and locks. Explicit deletes only: nothing here relies
    on, or changes, a cascade defined by the shared schema."""
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
    removed["accountUrls"] = await _count(delete(CustomerAccountUrls).where(CustomerAccountUrls.conversationId == conversation_id))
    removed["conversationCapabilities"] = await _count(delete(ConversationCapability).where(ConversationCapability.conversationId == conversation_id))
    # Exact keys (primary-key lookups), one per limit class that is keyed by conversation id.
    bucket_keys = [f"{limit_class}:{conversation_id}" for limit_class in _CONVERSATION_LIMIT_CLASSES]
    removed["rateLimitBuckets"] = await _count(text('DELETE FROM "RateLimitBucket" WHERE "key" = ANY(:keys)').bindparams(keys=bucket_keys))
    removed["conversations"] = await _count(delete(Conversation).where(Conversation.id == conversation_id))
    return DeletionResult(state=STATE_COMPLETED, removed={k: v for k, v in removed.items() if v}, minimized_commerce_records=minimized, unresolved_commerce_records=unresolved)


# ---------------------------------------------------------------------------
# Retention (used by scripts/data_retention.py; never scheduled or exposed over HTTP here)
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
    """Dry-run unless `execute` is true AND settings.retention_execution_enabled is true. One run
    at a time across all workers (advisory lock class 3); a second worker reports nothing done.
    Work is bounded: at most max_batches x batch_size conversations per run, each conversation in
    its own transaction, so a failure never reports success and a retry simply continues."""
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
    # 1. Deletions that were requested but could not finish (an operation was in flight, or a crash).
    pending = [p for p in (await session.execute(select(ConversationDeletion.pendingConversationId).where(ConversationDeletion.state == STATE_DELETING).order_by(ConversationDeletion.requestedAt).limit(size))).scalars() if p]
    _add(report.eligible, "pendingDeletions", len(pending))
    await session.commit()
    if execute:
        for conversation_id in pending:
            try:
                result = await complete_conversation_deletion(session, conversation_id)
            except Exception as err:  # noqa: BLE001
                await session.rollback()
                report.failed += 1
                logger.error("DATA_LIFECYCLE_RETENTION_ITEM_FAILED %s", json.dumps({"errorType": type(err).__name__}))
                continue
            if result.completed:
                _add(report.deleted, "pendingDeletionsCompleted", 1)
                report.minimized += result.minimized_commerce_records
            else:
                _add(report.held, "operationInFlight", 1)

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
                result = await request_conversation_deletion(session, conversation_id, origin=ORIGIN_RETENTION)
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
        ('SELECT count(*) FROM "ConversationDeletion" WHERE "state" = \'completed\' AND "expiresAt" < :cutoff', 'DELETE FROM "ConversationDeletion" WHERE "state" = \'completed\' AND "expiresAt" < :cutoff', "tombstones", {"cutoff": now}),
    ):
        count = await session.scalar(text(statement_count), params) or 0
        _add(report.eligible, name, count)
        if execute and count:
            _add(report.deleted, name, (await session.execute(text(statement_delete), params)).rowcount or 0)
            await session.commit()
    await session.commit()
