-- Phase 6 / 6A (data lifecycle, finding F11): deletion tombstones.
-- Additive only. The shared Prisma schema is untouched: no existing table, column, constraint or
-- cascade is changed. Apply after 0003. Python-owned table.
--
-- REVISED IN PHASE 6A before any application outside the disposable test database: the Phase 6
-- version carried a "pending" state and the pending conversation id. Deletion is now synchronous
-- and atomic, so a tombstone only ever records a COMPLETED deletion.
--
-- One row per conversation whose deletion completed (requested by its owner, or by retention).
-- It exists so that a writer already inside its transaction, a retry, another instance or a stale
-- cache can never write the conversation back afterwards. It holds NO customer content: a SHA-256
-- of the conversation id, an origin, counters and timestamps. Rows are removed by the retention
-- command once "expiresAt" has passed.

BEGIN;

CREATE TABLE IF NOT EXISTS "ConversationDeletion" (
    "conversationKey" TEXT PRIMARY KEY,
    "origin"          TEXT NOT NULL,
    "completedAt"     TIMESTAMP(3) WITHOUT TIME ZONE NOT NULL,
    "expiresAt"       TIMESTAMP(3) WITHOUT TIME ZONE NOT NULL,
    "heldRecords"     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS "ConversationDeletion_expiresAt_idx" ON "ConversationDeletion" ("expiresAt");

-- Retention scans need these to stay bounded (additive indexes on Python-owned tables only).
CREATE INDEX IF NOT EXISTS "ConversationCapability_expiresAt_idx" ON "ConversationCapability" ("expiresAt");
CREATE INDEX IF NOT EXISTS "BuildCapability_expiresAt_idx" ON "BuildCapability" ("expiresAt");

COMMIT;

-- Rollback (only if reverting Phase 6 entirely): drop the two capability indexes and the
-- "ConversationDeletion" table. Dropping the table removes resurrection protection for
-- deletions still inside their tombstone window; do it only after that window has passed.
