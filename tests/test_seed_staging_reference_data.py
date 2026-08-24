"""Safety-logic tests for scripts/seed_staging_reference_data.py. No live database or Odoo
credentials needed -- every asyncpg call is a fake in-memory stand-in."""

import json

import pytest

from scripts.seed_staging_reference_data import (
    DbIdentity,
    SeedAbort,
    _build_insert_sql,
    _jsonb_param,
    check_staging_empty,
    connect_readonly,
    copy_table,
    normalize_dsn,
    run_integrity_checks,
    safe_host,
    validate_destination,
)


class _NoopAsyncCM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False  # never suppress an exception


class FakeConn:
    """Minimal stand-in for asyncpg.Connection. fetchval_queue/fetch_queue are consumed in the
    exact order the code under test calls them -- matches how each function issues its queries."""

    def __init__(self, fetchval_queue=None, fetch_queue=None, executemany_error=None):
        self._fetchval_queue = list(fetchval_queue or [])
        self._fetch_queue = list(fetch_queue or [])
        self.executemany_error = executemany_error
        self.executemany_calls = []

    async def fetchval(self, *a, **kw):
        return self._fetchval_queue.pop(0)

    async def fetch(self, *a, **kw):
        return self._fetch_queue.pop(0)

    async def executemany(self, sql, records):
        self.executemany_calls.append((sql, records))
        if self.executemany_error:
            raise self.executemany_error

    def transaction(self, readonly=False):
        return _NoopAsyncCM()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_safe_host_never_includes_password():
    dsn = "postgresql+asyncpg://user:supersecret@db.example.com:5432/dua_scent_db"
    host = safe_host(dsn)
    assert host == "db.example.com:5432"
    assert "supersecret" not in host
    assert "user" not in host


def test_normalize_dsn_strips_sqlalchemy_driver_suffix():
    assert normalize_dsn("postgresql+asyncpg://u:p@h:5432/d") == "postgresql://u:p@h:5432/d"
    assert normalize_dsn("postgresql://u:p@h:5432/d") == "postgresql://u:p@h:5432/d"


def test_jsonb_param_passthrough_and_encoding():
    assert _jsonb_param(None) is None
    assert _jsonb_param("already-text-json") == "already-text-json"
    assert _jsonb_param({"a": 1}) == json.dumps({"a": 1})


def test_build_insert_sql_casts_only_known_json_columns():
    sql = _build_insert_sql("FragranceProduct", ["id", "title", "notesJson"])
    assert sql == 'INSERT INTO "FragranceProduct" ("id", "title", "notesJson") VALUES ($1, $2, $3::jsonb)'
    sql2 = _build_insert_sql("OdooOilMapping", ["id", "odooSku"])
    assert "::jsonb" not in sql2


# ---------------------------------------------------------------------------
# Destination-swap / identity guards
# ---------------------------------------------------------------------------

def test_destination_name_mismatch_aborts():
    source = DbIdentity(host="h1", database="dua_scent_db", user="u")
    dest = DbIdentity(host="h2", database="some_other_db", user="u")
    with pytest.raises(SeedAbort, match="ABORT IMMEDIATELY"):
        validate_destination(source, dest, expected_staging_db="dua_scent_ai_staging")


def test_source_and_destination_same_database_aborts():
    source = DbIdentity(host="shared-host", database="dua_scent_ai_staging", user="a")
    dest = DbIdentity(host="shared-host", database="dua_scent_ai_staging", user="b")
    with pytest.raises(SeedAbort, match="same host\\+database"):
        validate_destination(source, dest, expected_staging_db="dua_scent_ai_staging")


def test_valid_distinct_staging_destination_passes():
    source = DbIdentity(host="prod-host", database="dua_scent_db", user="a")
    dest = DbIdentity(host="staging-host", database="dua_scent_ai_staging", user="b")
    validate_destination(source, dest, expected_staging_db="dua_scent_ai_staging")  # no raise


# ---------------------------------------------------------------------------
# Connectivity
# ---------------------------------------------------------------------------

async def test_source_unreachable_aborts_cleanly(monkeypatch):
    import asyncpg

    async def _boom(*a, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(asyncpg, "connect", _boom)
    with pytest.raises(SeedAbort, match="SOURCE DATABASE UNREACHABLE — NOTHING COPIED"):
        await connect_readonly("postgresql://u:p@h/db", "source")


async def test_staging_unreachable_aborts_cleanly(monkeypatch):
    import asyncpg

    async def _boom(*a, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(asyncpg, "connect", _boom)
    with pytest.raises(SeedAbort, match="STAGING DATABASE UNREACHABLE — NOTHING COPIED"):
        await connect_readonly("postgresql://u:p@h/db", "staging")


# ---------------------------------------------------------------------------
# Destination already has data
# ---------------------------------------------------------------------------

async def test_staging_table_already_has_rows_aborts():
    conn = FakeConn(fetchval_queue=[7])
    with pytest.raises(SeedAbort, match='already has 7 row'):
        await check_staging_empty(conn, "FragranceProduct")


async def test_staging_table_empty_passes():
    conn = FakeConn(fetchval_queue=[0])
    await check_staging_empty(conn, "FragranceProduct")  # no raise


# ---------------------------------------------------------------------------
# copy_table
# ---------------------------------------------------------------------------

async def test_copy_table_empty_source_is_a_noop():
    source = FakeConn(fetch_queue=[[]])
    dest = FakeConn()
    result = await copy_table(source, dest, "FragranceProduct")
    assert result == (0, 0)
    assert dest.executemany_calls == []


async def test_copy_table_successful_copy():
    row = {"id": "p1", "title": "Test", "notesJson": {"top": ["rose"]}}
    source = FakeConn(fetch_queue=[[row]])
    dest = FakeConn(fetchval_queue=[1])  # post-copy COUNT(*) == source_count
    result = await copy_table(source, dest, "FragranceProduct")
    assert result == (1, 1)
    assert len(dest.executemany_calls) == 1
    sql, records = dest.executemany_calls[0]
    assert "::jsonb" in sql
    assert records[0][2] == json.dumps({"top": ["rose"]})  # jsonb column encoded, not passed as dict


async def test_copy_table_partial_failure_is_rolled_back_and_raises():
    source = FakeConn(fetch_queue=[[{"id": "p1"}]])
    dest = FakeConn(executemany_error=RuntimeError("unique_violation"))
    with pytest.raises(SeedAbort, match="rolled back"):
        await copy_table(source, dest, "FragranceProduct")


async def test_copy_table_row_count_mismatch_aborts():
    source = FakeConn(fetch_queue=[[{"id": "p1"}, {"id": "p2"}]])
    dest = FakeConn(fetchval_queue=[1])  # only 1 landed, source had 2
    with pytest.raises(SeedAbort, match="row-count mismatch"):
        await copy_table(source, dest, "FragranceProduct")


# ---------------------------------------------------------------------------
# Integrity checks
# ---------------------------------------------------------------------------

async def test_integrity_checks_all_clean():
    dest = FakeConn(fetchval_queue=[0, 0, 0, 0, 0])
    failures = await run_integrity_checks(dest)
    assert failures == []


async def test_integrity_checks_report_every_failure():
    # order: orphans, normalizedTitle dup, componentKey dup, fragranceProductId dup, region-summary dup
    dest = FakeConn(fetchval_queue=[2, 0, 0, 1, 3])
    failures = await run_integrity_checks(dest)
    assert len(failures) == 3
    assert any("OdooOilMapping" in f and "fragranceProductId not present" in f for f in failures)
    assert any("OdooOilMapping" in f and "duplicate" in f for f in failures)
    assert any("ProductRegionSummary" in f for f in failures)
