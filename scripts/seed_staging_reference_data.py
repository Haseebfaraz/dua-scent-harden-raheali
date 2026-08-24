"""One-time staging seed: copies ONLY four reference/catalog tables from the existing
(production) database into a new staging database. Never touches OrderHistory, Conversation,
Message, CustomerProfileState, FragranceRecommendation, RecommendationInventorySnapshot,
RecommendationInventoryComponent, Session, CustomerToken, CodeVerifier, or CustomerAccountUrls.

Pure Python (asyncpg, already a project dependency) -- no pg_dump/psql required. asyncpg talks
the Postgres wire protocol directly, so a plain SELECT-then-parameterized-INSERT round trip
(what this script does) needs nothing pg_dump/psql would give beyond convenience; the CLI tools
would only earn their keep for a full logical dump of an entire database, which this explicitly
is not. This also makes the tool identical to run on Windows, Render, Linux, or macOS -- no
shell-specific syntax anywhere.

Windows/PowerShell usage:
    $env:PROD_DATABASE_URL="postgresql://...production..."
    $env:STAGING_DATABASE_URL="postgresql://...staging..."
    $env:EXPECTED_STAGING_DATABASE="dua_scent_ai_staging"
    python scripts/seed_staging_reference_data.py

Optional, separate live-Odoo sanity check after a successful copy (never part of the DB
transaction -- a live HTTP call must never be able to affect whether the copy itself succeeds):
    python scripts/seed_staging_reference_data.py --check-odoo-sample 10

Afterwards, clear the secrets from your shell:
    Remove-Item Env:PROD_DATABASE_URL
    Remove-Item Env:STAGING_DATABASE_URL
    Remove-Item Env:EXPECTED_STAGING_DATABASE
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit

import asyncpg

logger = logging.getLogger("seed_staging")

TABLES_IN_ORDER = ["FragranceProduct", "OdooOilMapping", "ExistingCombination", "ProductRegionSummary"]

# jsonb columns on these tables -- verified against prisma/migrations/, no others exist on these
# four tables. Passed through as raw text with an explicit ::jsonb cast (see _jsonb_param) so the
# JSON is never re-serialized/re-ordered by a Python json.loads/dumps round trip.
JSON_COLUMNS = {"FragranceProduct": {"notesJson"}, "ExistingCombination": {"componentProductsJson"}}

DEFAULT_EXPECTED_STAGING_DATABASE = "dua_scent_ai_staging"


class SeedAbort(Exception):
    """Raised to stop the run immediately. The message is already safe to print verbatim --
    never put a password or a full connection string into one of these."""


@dataclass(frozen=True)
class DbIdentity:
    host: str
    database: str
    user: str


def normalize_dsn(dsn: str) -> str:
    """Accept the SQLAlchemy-style postgresql+asyncpg:// DSN the app's own .env files use, or a
    plain postgresql:// one -- asyncpg.connect() only understands the latter."""
    return dsn.replace("postgresql+asyncpg://", "postgresql://")


def safe_host(dsn: str) -> str:
    """host[:port] only -- never the password, never the full URL."""
    parts = urlsplit(normalize_dsn(dsn))
    host = parts.hostname or "?"
    return f"{host}:{parts.port}" if parts.port else host


async def connect_readonly(dsn: str, role: str) -> asyncpg.Connection:
    try:
        return await asyncpg.connect(normalize_dsn(dsn), timeout=10)
    except Exception as err:
        raise SeedAbort(f"{role.upper()} DATABASE UNREACHABLE — NOTHING COPIED") from err


async def fetch_identity(conn: asyncpg.Connection, dsn: str) -> DbIdentity:
    database = await conn.fetchval("SELECT current_database()")
    user = await conn.fetchval("SELECT current_user")
    return DbIdentity(host=safe_host(dsn), database=database, user=user)


def describe(identity: DbIdentity) -> str:
    return f"database={identity.database} host={identity.host} user={identity.user}"


def validate_destination(source: DbIdentity, dest: DbIdentity, expected_staging_db: str) -> None:
    """Hard guard against a swapped PROD_DATABASE_URL/STAGING_DATABASE_URL pair. Does not trust
    the *names* of the environment variables at all -- only what the live connections actually
    report about themselves."""
    if dest.database != expected_staging_db:
        raise SeedAbort(
            f'ABORT IMMEDIATELY: destination database is "{dest.database}", expected '
            f'"{expected_staging_db}" (set via EXPECTED_STAGING_DATABASE). Refusing to write.'
        )
    if (source.host, source.database) == (dest.host, dest.database):
        raise SeedAbort(
            "ABORT IMMEDIATELY: source and destination resolve to the same host+database. "
            "Refusing to treat one database as both the read source and the write target."
        )


async def table_row_count(conn: asyncpg.Connection, table: str) -> int:
    return await conn.fetchval(f'SELECT COUNT(*) FROM "{table}"')


async def check_staging_empty(conn: asyncpg.Connection, table: str) -> None:
    n = await table_row_count(conn, table)
    if n != 0:
        raise SeedAbort(
            f'ABORT IMMEDIATELY: staging."{table}" already has {n} row(s). This tool never '
            "truncates or overwrites automatically -- decide explicitly and clear it yourself first."
        )


def _jsonb_param(value):
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value)  # only reached if asyncpg ever hands back a decoded object instead of text


def _build_insert_sql(table: str, columns: list[str]) -> str:
    json_cols = JSON_COLUMNS.get(table, set())
    placeholders = [f"${i}::jsonb" if col in json_cols else f"${i}" for i, col in enumerate(columns, start=1)]
    col_list = ", ".join(f'"{c}"' for c in columns)
    return f'INSERT INTO "{table}" ({col_list}) VALUES ({", ".join(placeholders)})'


async def fetch_source_rows(conn: asyncpg.Connection, table: str) -> list[asyncpg.Record]:
    # readonly=True makes Postgres itself reject any write for the lifetime of this transaction --
    # not just "the code happens not to call INSERT/UPDATE/DELETE".
    async with conn.transaction(readonly=True):
        return await conn.fetch(f'SELECT * FROM "{table}"')


async def copy_table(source_conn: asyncpg.Connection, dest_conn: asyncpg.Connection, table: str) -> tuple[int, int]:
    """Returns (source_count, staging_count_after). Raises SeedAbort on any failure; the
    destination transaction is rolled back automatically as the exception propagates out of the
    `async with` block, so a failed table leaves staging exactly as it was before this call."""
    rows = await fetch_source_rows(source_conn, table)
    source_count = len(rows)
    if source_count == 0:
        logger.info('%s: source has 0 rows -- nothing to copy.', table)
        return 0, 0

    columns = list(rows[0].keys())
    insert_sql = _build_insert_sql(table, columns)
    records = [tuple(_jsonb_param(r[c]) for c in columns) for r in rows]

    try:
        async with dest_conn.transaction():
            await dest_conn.executemany(insert_sql, records)
    except Exception as err:
        raise SeedAbort(f'ABORT: copying "{table}" failed and was rolled back: {err}') from err

    dest_count = await table_row_count(dest_conn, table)
    if dest_count != source_count:
        raise SeedAbort(f'ABORT: "{table}" row-count mismatch after copy -- source={source_count} staging={dest_count}.')
    return source_count, dest_count


async def run_integrity_checks(dest_conn: asyncpg.Connection) -> list[str]:
    failures = []

    orphans = await dest_conn.fetchval(
        'SELECT COUNT(*) FROM "OdooOilMapping" o '
        'LEFT JOIN "FragranceProduct" p ON p.id = o."fragranceProductId" '
        "WHERE p.id IS NULL"
    )
    if orphans:
        failures.append(f'{orphans} OdooOilMapping row(s) reference a fragranceProductId not present in FragranceProduct')

    for table, col in [
        ("FragranceProduct", '"normalizedTitle"'),
        ("ExistingCombination", '"componentKey"'),
        ("OdooOilMapping", '"fragranceProductId"'),
    ]:
        n = await dest_conn.fetchval(f'SELECT COUNT(*) FROM (SELECT {col} FROM "{table}" GROUP BY {col} HAVING COUNT(*) > 1) d')
        if n:
            failures.append(f"{table}.{col} has {n} duplicate value(s)")

    n = await dest_conn.fetchval(
        'SELECT COUNT(*) FROM ('
        '  SELECT "normalizedProductName", scope, "scopeValue" FROM "ProductRegionSummary"'
        '  GROUP BY "normalizedProductName", scope, "scopeValue" HAVING COUNT(*) > 1'
        ") d"
    )
    if n:
        failures.append(f"ProductRegionSummary(normalizedProductName, scope, scopeValue) has {n} duplicate combination(s)")

    return failures


async def verify_odoo_sample(dest_conn: asyncpg.Connection, sample_size: int) -> None:
    """Deliberately separate from the copy transaction -- a live Odoo HTTP call is not a database
    operation and must never be able to affect whether the copy itself succeeds or rolls back.
    Reuses the app's own Odoo client/config (ODOO_INVENTORY_URL/ODOO_INVENTORY_API_KEY), never a
    second, duplicate HTTP implementation."""
    from app.integrations.odoo_client import get_inventory_by_skus

    rows = await dest_conn.fetch(
        'SELECT "odooSku" FROM "OdooOilMapping" WHERE active = true ORDER BY random() LIMIT $1', sample_size
    )
    skus = [r["odooSku"] for r in rows]
    if not skus:
        logger.info("Odoo sample check: no active OdooOilMapping rows to sample.")
        return

    result = await get_inventory_by_skus(skus)
    if not result.get("ok"):
        logger.warning(
            "Odoo sample check: request failed (%s) -- inconclusive, not necessarily a data problem.",
            result.get("error") or result.get("status"),
        )
        return

    found = {p.get("default_code") for p in (result["json"].get("products") or [])}
    missing = [s for s in skus if s not in found]
    logger.info("Odoo sample check: %d/%d sampled SKUs resolved live.", len(skus) - len(missing), len(skus))
    if missing:
        logger.warning("Not resolved by Odoo right now (stale mapping or sandbox drift): %s", missing)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--check-odoo-sample", type=int, default=0, metavar="N",
        help="After a successful copy, sample N OdooOilMapping rows against the live Odoo API. Off by default.",
    )
    return parser.parse_args(argv)


async def run(prod_dsn: str, staging_dsn: str, expected_staging_db: str, check_odoo_sample: int = 0) -> int:
    source_conn = dest_conn = None
    try:
        source_conn = await connect_readonly(prod_dsn, "source")
        source_id = await fetch_identity(source_conn, prod_dsn)
        logger.info("Source:      %s", describe(source_id))

        dest_conn = await connect_readonly(staging_dsn, "staging")
        dest_id = await fetch_identity(dest_conn, staging_dsn)
        logger.info("Destination: %s", describe(dest_id))

        validate_destination(source_id, dest_id, expected_staging_db)

        for table in TABLES_IN_ORDER:
            await check_staging_empty(dest_conn, table)

        logger.info("All safety checks passed. Copying %d tables in order...", len(TABLES_IN_ORDER))
        for table in TABLES_IN_ORDER:
            src_n, dst_n = await copy_table(source_conn, dest_conn, table)
            logger.info("  %s: source=%d staging_after=%d OK", table, src_n, dst_n)

        failures = await run_integrity_checks(dest_conn)
        if failures:
            logger.error("Integrity checks FAILED:")
            for f in failures:
                logger.error("  - %s", f)
            return 1
        logger.info("Integrity checks passed.")

        if check_odoo_sample:
            await verify_odoo_sample(dest_conn, check_odoo_sample)

        logger.info("Done. Source database was never written to.")
        return 0
    except SeedAbort as abort:
        logger.error(str(abort))
        return 1
    finally:
        if source_conn is not None:
            await source_conn.close()
        if dest_conn is not None:
            await dest_conn.close()


async def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args(argv)

    prod_dsn = os.environ.get("PROD_DATABASE_URL")
    staging_dsn = os.environ.get("STAGING_DATABASE_URL")
    expected_db = os.environ.get("EXPECTED_STAGING_DATABASE", DEFAULT_EXPECTED_STAGING_DATABASE)
    if not prod_dsn or not staging_dsn:
        logger.error("Set PROD_DATABASE_URL and STAGING_DATABASE_URL in your environment first (not as arguments).")
        return 2

    return await run(prod_dsn, staging_dsn, expected_db, args.check_odoo_sample)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
