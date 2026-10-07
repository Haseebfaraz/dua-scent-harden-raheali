"""Read-only schema compatibility check for ANY database (staging, shared production).

Compares the live schema with migrations/*.sql (scripts/schema_spec.py): the shared base tables
(0000, consolidated from the old Prisma schema) and the Python-owned tables (0001-0004). It only
runs SELECTs against information_schema / pg_indexes inside a READ ONLY transaction: it creates,
alters and writes nothing. It never prints the connection string.

    python -m scripts.check_schema_compat            # DATABASE_URL
    python -m scripts.check_schema_compat --json

Result per table: ok / missing table / missing column / type or nullability mismatch / extra
column (extra is a warning: another application may own it). Exit 1 if anything required is
missing or mismatched. Also reports which migrations still look unapplied.
"""

import argparse
import asyncio
import json
import os
import sys

import asyncpg

from scripts.schema_spec import expected_schema

PYTHON_OWNED = {
    "0001_build_capability": ["BuildCapability"],
    "0002_conversation_capability_and_rate_limits": ["ConversationCapability", "RateLimitBucket"],
    "0003_message_security_classification": ["MessageSecurityClassification"],
    "0004_conversation_deletion": ["ConversationDeletion"],
}


async def compare(conn) -> dict:
    spec, spec_indexes = expected_schema()
    rows = await conn.fetch(
        "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns WHERE table_schema = current_schema()"
    )
    live: dict[str, dict[str, tuple[str, str]]] = {}
    for r in rows:
        live.setdefault(r["table_name"], {})[r["column_name"]] = (r["data_type"], r["is_nullable"])
    indexes = {r["indexname"] for r in await conn.fetch("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")}
    report = {"errors": [], "warnings": [], "tables": {}, "unappliedMigrations": []}
    for table, columns in sorted(spec.items()):
        found = live.get(table)
        if found is None:
            report["tables"][table] = "missing"
            report["errors"].append(f"missing table {table}")
            continue
        problems = []
        for column, expected in columns.items():
            if column not in found:
                problems.append(f"missing column {column}")
            elif found[column] != expected:
                problems.append(f"{column}: expected {expected[0]}/{'NULL' if expected[1] == 'YES' else 'NOT NULL'}, found {found[column][0]}/{'NULL' if found[column][1] == 'YES' else 'NOT NULL'}")
        report["errors"].extend(f"{table}: {p}" for p in problems)
        extra = sorted(set(found) - set(columns))
        report["warnings"].extend(f"{table}: extra column {c} (not used by this service)" for c in extra)
        report["tables"][table] = "ok" if not problems else "mismatch"
    for name in sorted(spec_indexes - indexes):
        report["warnings"].append(f"missing index {name}")
    for migration, tables in PYTHON_OWNED.items():
        if any(t not in live for t in tables):
            report["unappliedMigrations"].append(migration)
    if "verifiedShopifyCustomerId" not in live.get("BuildCapability", {}) and "0002_conversation_capability_and_rate_limits" not in report["unappliedMigrations"]:
        report["unappliedMigrations"].append("0002_conversation_capability_and_rate_limits")
    return report


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL is not set")
        return 2
    conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    try:
        async with conn.transaction(readonly=True):
            report = await compare(conn)
    finally:
        await conn.close()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for line in report["errors"]:
            print("ERROR  ", line)
        for line in report["warnings"]:
            print("WARNING", line)
        if report["unappliedMigrations"]:
            print("Unapplied migrations:", ", ".join(report["unappliedMigrations"]))
        print("schema compatible" if not report["errors"] else f"{len(report['errors'])} incompatibility(ies)")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
