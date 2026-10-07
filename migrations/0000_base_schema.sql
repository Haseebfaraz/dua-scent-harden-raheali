-- Base schema for the SHARED business tables (originally owned by the Node app's Prisma schema).
--
-- Reviewed consolidation of the ten Prisma migrations in the reference repository
-- (Haseebfaraz/Scent-Ai-App, prisma/migrations, 20260724135208 .. 20260812070407, final state of
-- prisma/schema.prisma at f3dec51): same table names, column names, types, defaults, unique
-- indexes, indexes and foreign keys, with Prisma's own constraint/index names.
--
-- FOR A FRESH, EMPTY DATABASE ONLY (a new staging/dev/disposable database). It is idempotent
-- (CREATE ... IF NOT EXISTS everywhere, foreign keys added only when absent) and it never ALTERs,
-- drops or rewrites anything, so running it against an existing database creates nothing that
-- exists already -- but it also does NOT repair a partially migrated database. For an existing
-- database run the read-only compatibility check instead:
--     python -m scripts.check_schema_compat
-- Apply order: 0000, then 0001 .. 0004 (docs/FEATURE_RESTORATION.md, "Database").
--
-- Not created: Prisma's "_prisma_migrations" bookkeeping table (the Node app is retired).

BEGIN;

CREATE TABLE IF NOT EXISTS "Session" (
    "id" TEXT NOT NULL,
    "shop" TEXT NOT NULL,
    "state" TEXT NOT NULL,
    "isOnline" BOOLEAN NOT NULL DEFAULT false,
    "scope" TEXT,
    "expires" TIMESTAMP(3),
    "accessToken" TEXT NOT NULL,
    "userId" BIGINT,
    "firstName" TEXT,
    "lastName" TEXT,
    "email" TEXT,
    "accountOwner" BOOLEAN NOT NULL DEFAULT false,
    "locale" TEXT,
    "collaborator" BOOLEAN DEFAULT false,
    "emailVerified" BOOLEAN DEFAULT false,
    CONSTRAINT "Session_pkey" PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS "CustomerToken" (
    "id" TEXT NOT NULL,
    "conversationId" TEXT NOT NULL,
    "accessToken" TEXT NOT NULL,
    "refreshToken" TEXT,
    "expiresAt" TIMESTAMP(3) NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,
    CONSTRAINT "CustomerToken_pkey" PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS "CodeVerifier" (
    "id" TEXT NOT NULL,
    "state" TEXT NOT NULL,
    "verifier" TEXT NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "expiresAt" TIMESTAMP(3) NOT NULL,
    CONSTRAINT "CodeVerifier_pkey" PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS "Conversation" (
    "id" TEXT NOT NULL,
    "customerEmail" TEXT,
    "customerName" TEXT,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,
    CONSTRAINT "Conversation_pkey" PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS "Message" (
    "id" TEXT NOT NULL,
    "conversationId" TEXT NOT NULL,
    "role" TEXT NOT NULL,
    "content" TEXT NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "Message_pkey" PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS "CustomerAccountUrls" (
    "id" TEXT NOT NULL,
    "conversationId" TEXT NOT NULL,
    "mcpApiUrl" TEXT,
    "authorizationUrl" TEXT,
    "tokenUrl" TEXT,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,
    CONSTRAINT "CustomerAccountUrls_pkey" PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS "Note" (
    "id" TEXT NOT NULL,
    "name" TEXT NOT NULL,
    "position" TEXT NOT NULL,
    "density" DOUBLE PRECISION NOT NULL,
    "family" TEXT,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "Note_pkey" PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS "OrderHistory" (
    "id" TEXT NOT NULL,
    "orderDate" TEXT,
    "season" TEXT,
    "classification" TEXT,
    "notes" TEXT NOT NULL,
    "city" TEXT,
    "stateName" TEXT,
    "countryName" TEXT,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "customerKeyHash" TEXT,
    "normalizedProductName" TEXT,
    "productName" TEXT,
    CONSTRAINT "OrderHistory_pkey" PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS "FragranceProduct" (
    "id" TEXT NOT NULL,
    "handle" TEXT,
    "title" TEXT NOT NULL,
    "normalizedTitle" TEXT NOT NULL,
    "notesRaw" TEXT,
    "notesJson" JSONB,
    "fragranceFamily" TEXT,
    "collection" TEXT,
    "pricePer5ml" DOUBLE PRECISION,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,
    "inspirationBrand" TEXT,
    "inspirationName" TEXT,
    "isSingleInspiration" BOOLEAN NOT NULL DEFAULT false,
    "tagLine" TEXT,
    CONSTRAINT "FragranceProduct_pkey" PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS "ExistingCombination" (
    "id" TEXT NOT NULL,
    "title" TEXT NOT NULL,
    "normalizedTitle" TEXT NOT NULL,
    "type" TEXT NOT NULL,
    "componentProductsJson" JSONB NOT NULL,
    "componentKey" TEXT NOT NULL,
    "tagLine" TEXT,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "ExistingCombination_pkey" PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS "FragranceRecommendation" (
    "id" TEXT NOT NULL,
    "conversationId" TEXT NOT NULL,
    "customerProfileJson" JSONB NOT NULL,
    "productsJson" JSONB NOT NULL,
    "combinationType" TEXT NOT NULL,
    "scoreJson" JSONB NOT NULL,
    "evidenceJson" JSONB NOT NULL,
    "ratiosJson" JSONB NOT NULL,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "confirmedAt" TIMESTAMP(3),
    "shopifyProductId" TEXT,
    "customerFacingJson" JSONB,
    "evidenceScope" TEXT,
    "buildStatus" TEXT NOT NULL DEFAULT 'draft',
    "draftExcludedNotes" JSONB,
    "draftName" TEXT,
    "draftRatiosJson" JSONB,
    "shopifyVariantId" TEXT,
    CONSTRAINT "FragranceRecommendation_pkey" PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS "ProductRegionSummary" (
    "id" TEXT NOT NULL,
    "normalizedProductName" TEXT NOT NULL,
    "scope" TEXT NOT NULL,
    "scopeValue" TEXT NOT NULL,
    "orderCount" INTEGER NOT NULL,
    "distinctCustomerCount" INTEGER NOT NULL,
    "repeatCustomerCount" INTEGER NOT NULL,
    "updatedAt" TIMESTAMP(3) NOT NULL,
    CONSTRAINT "ProductRegionSummary_pkey" PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS "CustomerProfileState" (
    "id" TEXT NOT NULL,
    "conversationId" TEXT NOT NULL,
    "profileJson" JSONB NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,
    CONSTRAINT "CustomerProfileState_pkey" PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS "OdooOilMapping" (
    "id" TEXT NOT NULL,
    "fragranceProductId" TEXT NOT NULL,
    "odooSku" TEXT NOT NULL,
    "odooProductId" INTEGER,
    "odooVariantId" INTEGER,
    "odooName" TEXT,
    "unitOfMeasure" TEXT,
    "active" BOOLEAN NOT NULL DEFAULT true,
    "lastVerifiedAt" TIMESTAMP(3),
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,
    CONSTRAINT "OdooOilMapping_pkey" PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS "RecommendationInventorySnapshot" (
    "id" TEXT NOT NULL,
    "recommendationId" TEXT NOT NULL,
    "inventoryValidated" BOOLEAN NOT NULL,
    "buildable" BOOLEAN NOT NULL,
    "checkedAt" TIMESTAMP(3) NOT NULL,
    "oilTotalMl" DOUBLE PRECISION NOT NULL,
    "alcoholMl" DOUBLE PRECISION NOT NULL,
    "maxBuildableBottles" INTEGER,
    "limitingSku" TEXT,
    "requestStatus" TEXT NOT NULL,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "RecommendationInventorySnapshot_pkey" PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS "RecommendationInventoryComponent" (
    "id" TEXT NOT NULL,
    "snapshotId" TEXT NOT NULL,
    "fragranceProductId" TEXT,
    "productTitle" TEXT NOT NULL,
    "odooSku" TEXT,
    "ratioPercent" DOUBLE PRECISION NOT NULL,
    "requiredOilMl" DOUBLE PRECISION NOT NULL,
    "onHandQty" DOUBLE PRECISION,
    "mappingStatus" TEXT NOT NULL,
    "sufficient" BOOLEAN,
    "maxBuildableBottlesForComponent" INTEGER,
    CONSTRAINT "RecommendationInventoryComponent_pkey" PRIMARY KEY ("id")
);

-- Indexes (Prisma names). "OdooOilMapping_odooSku_key" is deliberately absent: migration
-- 20260811100419 dropped it (several products share one physical oil SKU).
CREATE INDEX IF NOT EXISTS "CustomerToken_conversationId_idx" ON "CustomerToken"("conversationId");
CREATE UNIQUE INDEX IF NOT EXISTS "CodeVerifier_state_key" ON "CodeVerifier"("state");
CREATE INDEX IF NOT EXISTS "CodeVerifier_state_idx" ON "CodeVerifier"("state");
CREATE INDEX IF NOT EXISTS "Message_conversationId_idx" ON "Message"("conversationId");
CREATE UNIQUE INDEX IF NOT EXISTS "CustomerAccountUrls_conversationId_key" ON "CustomerAccountUrls"("conversationId");
CREATE UNIQUE INDEX IF NOT EXISTS "Note_name_key" ON "Note"("name");
CREATE INDEX IF NOT EXISTS "OrderHistory_city_idx" ON "OrderHistory"("city");
CREATE INDEX IF NOT EXISTS "OrderHistory_stateName_idx" ON "OrderHistory"("stateName");
CREATE INDEX IF NOT EXISTS "OrderHistory_countryName_idx" ON "OrderHistory"("countryName");
CREATE INDEX IF NOT EXISTS "OrderHistory_normalizedProductName_idx" ON "OrderHistory"("normalizedProductName");
CREATE INDEX IF NOT EXISTS "OrderHistory_customerKeyHash_idx" ON "OrderHistory"("customerKeyHash");
CREATE UNIQUE INDEX IF NOT EXISTS "FragranceProduct_normalizedTitle_key" ON "FragranceProduct"("normalizedTitle");
CREATE INDEX IF NOT EXISTS "FragranceProduct_fragranceFamily_idx" ON "FragranceProduct"("fragranceFamily");
CREATE INDEX IF NOT EXISTS "FragranceProduct_collection_idx" ON "FragranceProduct"("collection");
CREATE UNIQUE INDEX IF NOT EXISTS "ExistingCombination_componentKey_key" ON "ExistingCombination"("componentKey");
CREATE INDEX IF NOT EXISTS "ExistingCombination_type_idx" ON "ExistingCombination"("type");
CREATE INDEX IF NOT EXISTS "FragranceRecommendation_conversationId_idx" ON "FragranceRecommendation"("conversationId");
CREATE INDEX IF NOT EXISTS "FragranceRecommendation_status_idx" ON "FragranceRecommendation"("status");
CREATE INDEX IF NOT EXISTS "ProductRegionSummary_scope_scopeValue_idx" ON "ProductRegionSummary"("scope", "scopeValue");
CREATE UNIQUE INDEX IF NOT EXISTS "ProductRegionSummary_normalizedProductName_scope_scopeValue_key" ON "ProductRegionSummary"("normalizedProductName", "scope", "scopeValue");
CREATE UNIQUE INDEX IF NOT EXISTS "CustomerProfileState_conversationId_key" ON "CustomerProfileState"("conversationId");
CREATE INDEX IF NOT EXISTS "CustomerProfileState_conversationId_idx" ON "CustomerProfileState"("conversationId");
CREATE UNIQUE INDEX IF NOT EXISTS "OdooOilMapping_fragranceProductId_key" ON "OdooOilMapping"("fragranceProductId");
CREATE INDEX IF NOT EXISTS "OdooOilMapping_odooSku_idx" ON "OdooOilMapping"("odooSku");
CREATE UNIQUE INDEX IF NOT EXISTS "RecommendationInventorySnapshot_recommendationId_key" ON "RecommendationInventorySnapshot"("recommendationId");
CREATE INDEX IF NOT EXISTS "RecommendationInventorySnapshot_recommendationId_idx" ON "RecommendationInventorySnapshot"("recommendationId");
CREATE INDEX IF NOT EXISTS "RecommendationInventoryComponent_snapshotId_idx" ON "RecommendationInventoryComponent"("snapshotId");

-- Foreign keys (added only when absent; PostgreSQL has no ADD CONSTRAINT IF NOT EXISTS).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'Message_conversationId_fkey' AND connamespace = current_schema()::regnamespace) THEN
        ALTER TABLE "Message" ADD CONSTRAINT "Message_conversationId_fkey" FOREIGN KEY ("conversationId") REFERENCES "Conversation"("id") ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'RecommendationInventorySnapshot_recommendationId_fkey' AND connamespace = current_schema()::regnamespace) THEN
        ALTER TABLE "RecommendationInventorySnapshot" ADD CONSTRAINT "RecommendationInventorySnapshot_recommendationId_fkey" FOREIGN KEY ("recommendationId") REFERENCES "FragranceRecommendation"("id") ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'RecommendationInventoryComponent_snapshotId_fkey' AND connamespace = current_schema()::regnamespace) THEN
        ALTER TABLE "RecommendationInventoryComponent" ADD CONSTRAINT "RecommendationInventoryComponent_snapshotId_fkey" FOREIGN KEY ("snapshotId") REFERENCES "RecommendationInventorySnapshot"("id") ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;
END $$;

COMMIT;
