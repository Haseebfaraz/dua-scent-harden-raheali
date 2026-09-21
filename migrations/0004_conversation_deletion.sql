-- Phase 6 (data lifecycle, finding F11): deletion tombstones.
-- Additive only. The shared Prisma schema is untouched: no existing table, column, constraint or
-- cascade is changed. Apply after 0003. Python-owned table.
--
-- One row per conversation whose deletion was requested (by its owner) or carried out (by
-- retention). It exists so that a request already in flight, a retry, another instance or a stale
-- cache can never write the conversation back after deletion was reported. It holds NO customer
-- content: a SHA-256 of the conversation id, a state, counters and timestamps (plus, only while a
-- deletion is still pending, the id it has to finish). Rows are
-- removed by the retention command once "expiresAt" has passed.

BEGIN;

CREATE TABLE IF NOT EXISTS "ConversationDeletion" (
    "conversationKey" TEXT PRIMARY KEY,
    -- Set ONLY while state = 'deleting' (the conversation still exists then, so this adds nothing),
    -- so that an interrupted deletion can be finished. Cleared to NULL on completion.
    "pendingConversationId" TEXT,
    "state"           TEXT NOT NULL,
    "origin"          TEXT NOT NULL,
    "requestedAt"     TIMESTAMP(3) WITHOUT TIME ZONE NOT NULL,
    "completedAt"     TIMESTAMP(3) WITHOUT TIME ZONE,
    "expiresAt"       TIMESTAMP(3) WITHOUT TIME ZONE,
    "heldRecords"     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS "ConversationDeletion_state_idx" ON "ConversationDeletion" ("state");
CREATE INDEX IF NOT EXISTS "ConversationDeletion_expiresAt_idx" ON "ConversationDeletion" ("expiresAt");

-- Retention scans need these to stay bounded (additive indexes on Python-owned tables only).
CREATE INDEX IF NOT EXISTS "ConversationCapability_expiresAt_idx" ON "ConversationCapability" ("expiresAt");
CREATE INDEX IF NOT EXISTS "BuildCapability_expiresAt_idx" ON "BuildCapability" ("expiresAt");

COMMIT;

-- Rollback (only if reverting Phase 6 entirely): drop the two capability indexes and the
-- "ConversationDeletion" table. Dropping the table removes resurrection protection for
-- deletions still inside their tombstone window; do it only after that window has passed.
