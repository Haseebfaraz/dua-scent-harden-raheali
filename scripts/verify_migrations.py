"""Migration verification (Phase 7, F12). DISPOSABLE DATABASES ONLY.

Creates the synthetic base schema the shared (Prisma-owned) tables would have, applies
migrations/0001..0004 in order, applies them a second time (they must be idempotent), and checks
the tables, columns, indexes and constraints the application relies on. Exit 1 on any mismatch.
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
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db.models import Base

    engine = create_async_engine(url.replace("postgresql://", "postgresql+asyncpg://", 1))
    async with engine.begin() as connection:
        # The synthetic base schema: the shared tables as the ORM models describe them. Python-owned
        # tables are dropped first so the migrations, not create_all, are what create them.
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    failures: list[str] = []
    try:
        for table in EXPECTED_TABLES:
            await conn.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
        files = sorted(glob.glob("migrations/*.sql"))
        if [os.path.basename(f)[:4] for f in files] != ["0001", "0002", "0003", "0004"]:
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
        indexes = {r["indexname"] for r in await conn.fetch("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")}
        for name in EXPECTED_INDEXES - indexes:
            failures.append(f"missing index {name}")
        uniques = {(r["table_name"], r["column_name"]) for r in await conn.fetch(
            "SELECT tc.table_name, kcu.column_name FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name WHERE tc.constraint_type = 'UNIQUE'")}
        for pair in EXPECTED_UNIQUE - uniques:
            failures.append(f"missing unique constraint {pair}")
        cascade = await conn.fetchval("SELECT confdeltype FROM pg_constraint WHERE conrelid = '\"MessageSecurityClassification\"'::regclass AND contype = 'f'")
        if (cascade.decode() if isinstance(cascade, bytes) else cascade) != "c":
            failures.append("MessageSecurityClassification -> Message is not ON DELETE CASCADE")
        # Application models must match what the migrations built.
        for table in EXPECTED_TABLES:
            model_columns = set(Base.metadata.tables[table].columns.keys())
            if model_columns != EXPECTED_TABLES[table]:
                failures.append(f"model {table} columns {sorted(model_columns ^ EXPECTED_TABLES[table])} differ from migration")
    finally:
        await conn.close()
    for failure in failures:
        print("FAIL", failure)
    print("migrations verified" if not failures else f"{len(failures)} problem(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
