"""Migration verification (Phase 7, F12). DISPOSABLE DATABASES ONLY.

Provisions the shared base schema from migrations/0000_base_schema.sql (the reviewed consolidation
of the old Prisma migrations), applies migrations/0001..0004 in order, applies the whole set a
second time (every file must be idempotent), and checks every table's columns, types and
nullability against migrations/*.sql (scripts/schema_spec.py), the indexes and constraints the
application relies on, and that the ORM models match. Exit 1 on any mismatch.
It refuses to run unless DATABASE_URL points at localhost / a unix socket, and never prints the URL.
"""

import asyncio
import glob
import os
import sys
from urllib.parse import urlparse

import asyncpg

EXPECTED_TABLES = {
    "BuildCapability": {"id", "recommendationId", "conversationId", "shop", "tokenHash", "verifiedShopifyCustomerId", "expiresAt", "revokedAt", "createdAt"},
    "ConversationCapability": {"id", "conversationId", "tokenHash", "verifiedShopifyCustomerId", "expiresAt", "revokedAt", "lastUsedAt", "createdAt"},
    "RateLimitBucket": {"key", "windowStart", "count", "updatedAt"},
    "MessageSecurityClassification": {"id", "messageId", "classification", "reasonCode", "classifierVersion", "createdAt"},
    "ConversationDeletion": {"conversationKey", "origin", "completedAt", "expiresAt", "heldRecords"},
}
EXPECTED_INDEXES = {"ConversationDeletion_expiresAt_idx", "ConversationCapability_expiresAt_idx", "BuildCapability_expiresAt_idx"}
EXPECTED_UNIQUE = {("BuildCapability", "tokenHash"), ("ConversationCapability", "tokenHash"), ("MessageSecurityClassification", "messageId")}


def _local(url: str) -> bool:
    parsed = urlparse(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    host = parsed.hostname
    return host in (None, "localhost", "127.0.0.1", "::1") or "host=/" in (parsed.query or "")


def _sql(path: str) -> str:
    return "\n".join(line for line in open(path).read().splitlines() if not line.strip().startswith("--"))


async def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    if not url or not _local(url):
        print("refusing: DATABASE_URL must point at a local disposable database")
        return 1
    from app.db.models import Base
    from scripts.schema_spec import expected_schema

    conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    failures: list[str] = []
    try:
        spec, spec_indexes = expected_schema()
        # Disposable database only (checked above): start from nothing so the migrations alone
        # are what build the schema.
        for table in spec:
            await conn.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
        files = sorted(glob.glob("migrations/*.sql"))
        if [os.path.basename(f)[:4] for f in files] != ["0000", "0001", "0002", "0003", "0004"]:
            failures.append(f"unexpected migration set: {[os.path.basename(f) for f in files]}")
        for round_ in (1, 2):  # the second application must be a no-op
            for path in files:
                try:
                    await conn.execute(_sql(path))
                except Exception as err:  # noqa: BLE001
                    failures.append(f"{os.path.basename(path)} round {round_}: {type(err).__name__}")
        for table, columns in EXPECTED_TABLES.items():
            found = {r["column_name"] for r in await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = $1", table)}
            if found != columns:
                failures.append(f"{table}: columns {sorted(found ^ columns)} differ")
        for table, columns in spec.items():
            rows = await conn.fetch("SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_schema = 'public' AND table_name = $1", table)
            found = {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in rows}
            if found != columns:
                failures.append(f"{table}: schema differs from migrations: {sorted(set(found.items()) ^ set(columns.items()))}")
        indexes = {r["indexname"] for r in await conn.fetch("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")}
        for name in (EXPECTED_INDEXES | spec_indexes) - indexes:
            failures.append(f"missing index {name}")
        uniques = {(r["table_name"], r["column_name"]) for r in await conn.fetch(
            "SELECT tc.table_name, kcu.column_name FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name WHERE tc.constraint_type = 'UNIQUE'")}
        for pair in EXPECTED_UNIQUE - uniques:
            failures.append(f"missing unique constraint {pair}")
        cascade = await conn.fetchval("SELECT confdeltype FROM pg_constraint WHERE conrelid = '\"MessageSecurityClassification\"'::regclass AND contype = 'f'")
        if (cascade.decode() if isinstance(cascade, bytes) else cascade) != "c":
            failures.append("MessageSecurityClassification -> Message is not ON DELETE CASCADE")
        # Application models must match what the migrations built (every mirrored table).
        for table, model in Base.metadata.tables.items():
            if table not in spec:
                failures.append(f"model {table} has no migration")
            elif set(model.columns.keys()) != set(spec[table]):
                failures.append(f"model {table} columns {sorted(set(model.columns.keys()) ^ set(spec[table]))} differ from migration")
        fks = {r["conname"]: r["confdeltype"] for r in await conn.fetch("SELECT conname, confdeltype FROM pg_constraint WHERE contype = 'f'")}
        for name in ("Message_conversationId_fkey", "RecommendationInventorySnapshot_recommendationId_fkey", "RecommendationInventoryComponent_snapshotId_fkey"):
            kind = fks.get(name)
            if (kind.decode() if isinstance(kind, bytes) else kind) != "c":
                failures.append(f"foreign key {name} missing or not ON DELETE CASCADE")
    finally:
        await conn.close()
    for failure in failures:
        print("FAIL", failure)
    print("migrations verified" if not failures else f"{len(failures)} problem(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
