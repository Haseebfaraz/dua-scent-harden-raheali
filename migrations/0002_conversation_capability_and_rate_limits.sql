-- Phase 2 (security hardening): conversation ownership capabilities, DB-backed rate limiting,
-- and verified-customer binding on build capabilities.
-- Additive only. The shared Prisma schema is untouched. Apply after 0001, before deploying the
-- Phase 2 code (staging first). Without it every public chat request fails closed.

BEGIN;

-- One row per issued conversation session secret. Only the SHA-256 hash of the secret is stored.
CREATE TABLE IF NOT EXISTS "ConversationCapability" (
    "id"                        TEXT PRIMARY KEY,
    "conversationId"            TEXT NOT NULL REFERENCES "Conversation"("id") ON DELETE CASCADE,
    "tokenHash"                 TEXT NOT NULL UNIQUE,
    -- Set only when a Shopify-signed logged_in_customer_id was bound to this conversation.
    "verifiedShopifyCustomerId" TEXT NULL,
    "expiresAt"                 TIMESTAMP(3) WITHOUT TIME ZONE NOT NULL,
    "revokedAt"                 TIMESTAMP(3) WITHOUT TIME ZONE NULL,
    "lastUsedAt"                TIMESTAMP(3) WITHOUT TIME ZONE NULL,
    "createdAt"                 TIMESTAMP(3) WITHOUT TIME ZONE NOT NULL
);

CREATE INDEX IF NOT EXISTS "ConversationCapability_conversationId_idx" ON "ConversationCapability" ("conversationId");

-- Fixed-window counters shared by every service instance. "key" is a class plus a keyed hash of
-- the subject (never a raw IP). Rows are tiny and stale ones are pruned opportunistically by
-- app/services/rate_limit.py (retention: two days after last update).
CREATE TABLE IF NOT EXISTS "RateLimitBucket" (
    "key"         TEXT PRIMARY KEY,
    "windowStart" TIMESTAMP(3) WITHOUT TIME ZONE NOT NULL,
    "count"       INTEGER NOT NULL,
    "updatedAt"   TIMESTAMP(3) WITHOUT TIME ZONE NOT NULL
);

CREATE INDEX IF NOT EXISTS "RateLimitBucket_updatedAt_idx" ON "RateLimitBucket" ("updatedAt");

-- A build capability can be bound to the Shopify-signed customer that first used it. Once bound,
-- a different signed customer can no longer use it even with the token.
ALTER TABLE "BuildCapability" ADD COLUMN IF NOT EXISTS "verifiedShopifyCustomerId" TEXT NULL;

COMMIT;

-- Rollback (only if reverting Phase 2 entirely): drop "ConversationCapability" and
-- "RateLimitBucket", and drop the "verifiedShopifyCustomerId" column from "BuildCapability".
-- Dropping ConversationCapability signs every guest out of their chat session.
