-- Phase 1 (security hardening): build capability tokens for Shopify build mutations.
-- Additive only. The shared Prisma schema is untouched. This table is owned by the Python service.
-- Apply once per database (staging first), before deploying the Phase 1 code -- without it every
-- preview/save/add-to-cart flow fails closed (no capability means no mutation).

BEGIN;

CREATE TABLE IF NOT EXISTS "BuildCapability" (
    "id"               TEXT PRIMARY KEY,
    "recommendationId" TEXT NOT NULL REFERENCES "FragranceRecommendation"("id") ON DELETE CASCADE,
    "conversationId"   TEXT NOT NULL,
    "shop"             TEXT NOT NULL,
    -- SHA-256 hex of the plaintext token. The plaintext is never stored or logged.
    "tokenHash"        TEXT NOT NULL UNIQUE,
    "expiresAt"        TIMESTAMP(3) WITHOUT TIME ZONE NOT NULL,
    "revokedAt"        TIMESTAMP(3) WITHOUT TIME ZONE NULL,
    "createdAt"        TIMESTAMP(3) WITHOUT TIME ZONE NOT NULL
);

CREATE INDEX IF NOT EXISTS "BuildCapability_recommendationId_idx" ON "BuildCapability" ("recommendationId");

COMMIT;

-- Rollback (only if reverting the Phase 1 deployment entirely): drop the "BuildCapability" table.
-- Dropping it invalidates every outstanding preview link. Customers regenerate one via the chat.
