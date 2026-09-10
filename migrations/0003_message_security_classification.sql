-- Phase 4 (security hardening): per-message scope/security classification.
-- Additive only. The shared Prisma schema is untouched. Apply after 0002. The chat works
-- without it only in degraded form (no persisted classification means legacy screening applies
-- to every replayed message), so apply before deploying the Phase 4 code.

BEGIN;

-- One row per classified customer message. Operational reason codes only: never the
-- classifier's prompt, never free-text explanations, never the message itself.
CREATE TABLE IF NOT EXISTS "MessageSecurityClassification" (
    "id"                TEXT PRIMARY KEY,
    "messageId"         TEXT NOT NULL UNIQUE REFERENCES "Message"("id") ON DELETE CASCADE,
    "classification"    TEXT NOT NULL,
    "reasonCode"        TEXT NOT NULL,
    "classifierVersion" TEXT NOT NULL,
    "createdAt"         TIMESTAMP(3) WITHOUT TIME ZONE NOT NULL
);

COMMIT;

-- Rollback (only if reverting Phase 4 entirely): drop the "MessageSecurityClassification" table.
