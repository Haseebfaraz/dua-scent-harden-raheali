"""Reference-data importer: dry runs keep nothing, re-runs are idempotent, keys/normalization match
the application, mojibake is repaired, skipped rows are explained, summaries regenerate exactly.
Runs in a throwaway PostgreSQL schema built from migrations/0000_base_schema.sql, so the shared
test tables are never touched. Synthetic fixtures only; nothing here targets a real database."""

import csv
import json
import os
import uuid
import zipfile
from xml.sax.saxutils import escape

import asyncpg
import pytest

from app.fragrance.combination_key import create_combination_key
from scripts import data_import as di
from scripts.note_encoding import MOJIBAKE

SALT = "test-salt"


def write_xlsx(path: str, sheets: dict[str, list[list]]) -> None:
    """A minimal real .xlsx (shared strings + numeric cells), like the production exports."""
    strings: list[str] = []

    def cell(ref: str, value) -> str:
        if value is None:
            return ""
        if isinstance(value, (int, float)):
            return f'<c r="{ref}"><v>{value}</v></c>'
        strings.append(str(value))
        return f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>'

    def col(i: int) -> str:
        name = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            name = chr(65 + r) + name
        return name

    sheet_xml = {}
    for name, rows in sheets.items():
        body = "".join(f'<row r="{r + 1}">' + "".join(cell(f"{col(c)}{r + 1}", v) for c, v in enumerate(row)) + "</row>" for r, row in enumerate(rows))
        sheet_xml[name] = f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{body}</sheetData></worksheet>'
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>' + "".join(f'<sheet name="{n}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(sheets)) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(f'<Relationship Id="rId{i + 1}" Type="x" Target="worksheets/sheet{i + 1}.xml"/>' for i in range(len(sheets))) + "</Relationships>")
        for i, name in enumerate(sheets):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", sheet_xml[name])
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{main}">' + "".join(f"<si><t>{escape(s)}</t></si>" for s in strings) + "</sst>")


def combo_row(title: str, components: list) -> list:
    row = ["h-" + title.lower().replace(" ", "-"), title, f"Hybrid: {title}"]
    for c in components:
        row += ["Inspiration", c, "Brand"]
    return row


@pytest.fixture
def catalog_xlsx(tmp_path):
    path = str(tmp_path / "catalog.xlsx")
    write_xlsx(path, {
        "Notes-Extraction-Separated": [
            ["Handle", "Title", "Notes", "PricePer5ml", "Collection", "Oil SKU", "Oil SKU", "Oil SKU", "Oil Sku"],
            ["the-opera", "The Opera", f"Rose, Crï{chr(191)}{chr(189)}me Brï{chr(191)}{chr(189)}lï{chr(191)}{chr(189)}e, Musk", 18.68, "Inspiration", "DUA-THOPRA_Oil"],
            ["water-of-arabia", "Water of Arabia", "Mandarin, Bergamot", 17, "Inspiration", "DUA-WATEAR_Oil"],
            ["black-widow-se", "Black Widow SE", "Oud", 20, "Inspiration", "DUA-WATEAR_Oil"],
            ["combo", "Opera Arabia", "Rose", 25, "Hybrid", "DUA-THOPRA_Oil", "DUA-WATEAR_Oil"],
            ["no-sku", "No Sku Product", "Amber", 15, "Dua Original"],
            [46095, 3.14, "Bad", 1, "Inspiration", "X"],
        ],
        "Inspirations": [
            ["INSPIRED EXPRESSION"], ["Handle", "Title", "Tag Line", "Inspiration", "Brand"],
            ["the-opera", "The Opera", "Inspiration: Opera by Sospiro", "Opera", "Sospiro"],
            ["water-of-arabia", "Water Of Arabia!", "Inspiration: Silver Mountain Water by Creed", "Silver Mountain Water", "Creed"],
            ["unknown", "Unknown Thing", "x", "y", "z"],
        ],
        "Hybrid": [
            ["INSPIRED HYBRID"], ["Handle", "Title", "Tag Line"],
            combo_row("Opera Arabia", ["The Opera", "Water of Arabia"]),
            combo_row("Arabia Opera Again", ["Water of Arabia", "The Opera"]),  # same components, different title: collision
            combo_row("Half Empty", ["The Opera", None]),
            combo_row("Ghost Pair", ["The Opera", "Not In Catalog"]),
        ],
        "Tribrid": [["INSPIRED TRIBRID"], ["Handle", "Title"]],
        "Quadbrid": [["INSPIRED QUADBRID"], ["Handle", "Title"]],
    })
    return path


@pytest.fixture
async def conn():
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    schema = f"import_test_{uuid.uuid4().hex[:10]}"
    connection = await asyncpg.connect(url)
    await connection.execute(f'CREATE SCHEMA "{schema}"')
    await connection.execute(f'SET search_path TO "{schema}"')
    sql = open(os.path.join("migrations", "0000_base_schema.sql"), encoding="utf-8").read()
    await connection.execute("\n".join(line for line in sql.splitlines() if not line.strip().startswith("--")))
    try:
        yield connection
    finally:
        await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await connection.close()


async def _run(conn, coroutine_factory, *, apply: bool):
    report = di.Report("test")
    tr = conn.transaction()
    await tr.start()
    try:
        await coroutine_factory(report)
    finally:
        await (tr.commit() if apply else tr.rollback())
    return report


async def _catalog(conn, path, apply=True):
    async def go(report):
        await di.import_products(conn, path, report)
        await di.import_inspirations(conn, path, report)
        await di.import_combinations(conn, path, report)
    return await _run(conn, go, apply=apply)


async def test_dry_run_reports_counts_and_keeps_nothing(conn, catalog_xlsx):
    report = await _catalog(conn, catalog_xlsx, apply=False)
    assert report.counts["products_inserted"] == 5
    assert report.skipped["title_missing_or_not_text"] == 1  # the 3.14 / date row
    assert await conn.fetchval('SELECT count(*) FROM "FragranceProduct"') == 0


async def test_catalog_import_is_idempotent_and_matches_application_keys(conn, catalog_xlsx):
    first = await _catalog(conn, catalog_xlsx)
    second = await _catalog(conn, catalog_xlsx)
    assert first.counts["products_inserted"] == 5 and second.counts["products_unchanged"] == 5 and not second.counts["products_updated"]
    assert first.counts["matched_by_title"] == 2 and not first.counts["matched_by_handle"]  # "Water Of Arabia!" normalizes to the catalog title
    assert first.skipped["no_catalog_match"] == 1 and second.counts["inspirations_unchanged"] == 2
    assert first.counts["hybrid_inserted"] == 2 and second.counts["hybrid_unchanged"] == 2
    assert first.skipped["hybrid_component_key_collision"] == 1 and first.skipped["hybrid_component_blank"] == 1
    assert first.counts["hybrid_rows_with_unresolved_components"] == 1
    opera = await conn.fetchrow('SELECT "notesRaw", "notesJson", "pricePer5ml", "inspirationName", "isSingleInspiration" FROM "FragranceProduct" WHERE "normalizedTitle" = $1', "the opera")
    assert opera["notesRaw"] == "Rose, Crème Brûlée, Musk" and json.loads(opera["notesJson"]) == ["Rose", "Crème Brûlée", "Musk"]
    assert opera["pricePer5ml"] == 18.68 and opera["inspirationName"] == "Opera" and opera["isSingleInspiration"] is True
    keys = {r["componentKey"] for r in await conn.fetch('SELECT "componentKey" FROM "ExistingCombination"')}
    assert create_combination_key(["The Opera", "Water of Arabia"]) in keys
    assert await conn.fetchval('SELECT count(*) FROM "FragranceProduct" WHERE "notesRaw" LIKE $1', f"%{MOJIBAKE}%") == 0


async def test_oil_sku_mapping_import(conn, catalog_xlsx):
    await _catalog(conn, catalog_xlsx)
    first = await _run(conn, lambda r: di.import_oil_skus(conn, catalog_xlsx, r), apply=True)
    second = await _run(conn, lambda r: di.import_oil_skus(conn, catalog_xlsx, r), apply=True)
    assert first.counts["mappings_inserted"] == 3 and second.counts["mappings_unchanged"] == 3
    assert first.skipped["multi_component_combo"] == 1 and first.skipped["no_sku"] == 1 and first.skipped["malformed_row"] == 1
    assert first.counts["skus_shared_by_several_products"] == 1  # two editions share one oil: allowed


def _write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


async def test_notes_order_history_and_summaries(conn, tmp_path, catalog_xlsx):
    await _catalog(conn, catalog_xlsx)
    orders = str(tmp_path / "orders.csv")
    _write_csv(orders, di.ORDER_COLUMNS, [
        ["2024-01-01", "Summer", "CLASSIFICATION: Floral", "Rose, Musk", "Austin", "Texas", "United States", "The Opera", "Ann Smith"],
        ["2024-02-01", "Summer", "Floral", "Rose", "Austin", "Texas", "United States", "The Opera", " ann smith "],  # same customer
        ["2024-03-01", "Winter", "Woody", "Rose, Oud", "Dallas", "Texas", "United States", "The Opera", "Bob"],
        ["2024-03-02", "Winter", "", "Oud", "", "", "Canada", "Water of Arabia", ""],
    ])
    notes = str(tmp_path / "notes.csv")
    _write_csv(notes, ["Notes", "Top", "Middle", "Base", "Average Density"], [["Rose", "1", "5", "2", "0.9"], ["Oud", "0", "1", "7", "1.2x"], ["", "1", "1", "1", "1"]])

    report = await _run(conn, lambda r: di.import_notes(conn, notes, orders, r), apply=True)
    assert report.counts["notes_inserted"] == 2 and report.skipped["name_missing"] == 1
    rose = await conn.fetchrow('SELECT position, density, family FROM "Note" WHERE name = $1', "Rose")
    assert (rose["position"], rose["density"], rose["family"]) == ("middle", 0.9, "Floral")

    with pytest.raises(di.ImportAbort):
        await _run(conn, lambda r: di.import_order_history(conn, orders, False, "", r), apply=False)  # no salt
    report = await _run(conn, lambda r: di.import_order_history(conn, orders, False, SALT, r), apply=True)
    assert report.counts["rows_inserted"] == 4 and report.counts["rows_without_customer_key"] == 1
    with pytest.raises(di.ImportAbort):
        await _run(conn, lambda r: di.import_order_history(conn, orders, False, SALT, r), apply=False)  # never appended twice
    hashes = [r["customerKeyHash"] for r in await conn.fetch('SELECT "customerKeyHash" FROM "OrderHistory" WHERE city = $1 ORDER BY "orderDate"', "Austin")]
    assert hashes[0] == hashes[1] == di.hash_customer_key("Ann Smith", SALT)
    assert await conn.fetchval('SELECT count(*) FROM "OrderHistory" WHERE classification = $1', "Floral") == 2  # prefix stripped

    report = await _run(conn, lambda r: di.build_summaries(conn, False, r), apply=True)
    state = await conn.fetchrow('SELECT "orderCount", "distinctCustomerCount", "repeatCustomerCount" FROM "ProductRegionSummary" WHERE scope = $1 AND "scopeValue" = $2 AND "normalizedProductName" = $3', "state", "Texas", "the opera")
    assert tuple(state) == (3, 2, 1)
    canada = await conn.fetchval('SELECT count(*) FROM "ProductRegionSummary" WHERE "scopeValue" = $1', "Canada")
    assert canada == 0  # rows without a customer key are excluded from regional counts, as before
    first_total = await conn.fetchval('SELECT count(*) FROM "ProductRegionSummary"')
    await conn.execute('''INSERT INTO "ProductRegionSummary" VALUES ('stale', 'gone', 'country', 'Nowhere', 1, 1, 0, now() - interval '1 day')''')
    again = await _run(conn, lambda r: di.build_summaries(conn, False, r), apply=True)
    assert again.counts["stale_rows"] == 1 and await conn.fetchval('SELECT count(*) FROM "ProductRegionSummary"') == first_total + 1
    pruned = await _run(conn, lambda r: di.build_summaries(conn, True, r), apply=True)
    assert pruned.counts["stale_rows_deleted"] == 1 and await conn.fetchval('SELECT count(*) FROM "ProductRegionSummary"') == first_total

    replaced = await _run(conn, lambda r: di.import_order_history(conn, orders, True, SALT, r), apply=True)
    assert replaced.counts["rows_replaced"] == 4 and await conn.fetchval('SELECT count(*) FROM "OrderHistory"') == 4

    checks = await _run(conn, lambda r: di.validate(conn, r), apply=False)
    assert checks.counts["products"] == 5 and checks.counts["order_history_rows"] == 4 and checks.counts["notes"] == 2


async def test_fix_note_encoding_repairs_stored_rows(conn):
    await conn.execute('''INSERT INTO "FragranceProduct" (id, title, "normalizedTitle", "notesRaw", "notesJson", "updatedAt") VALUES ('p1', 'X', 'x', $1, '[]', now())''', f"Yerba Mat{MOJIBAKE}, Rose")
    report = await _run(conn, lambda r: di.repair_note_encoding(conn, r), apply=True)
    row = await conn.fetchrow('SELECT "notesRaw", "notesJson" FROM "FragranceProduct" WHERE id = $1', "p1")
    assert report.counts["products_fixed"] == 1 and row["notesRaw"] == "Yerba Maté, Rose" and json.loads(row["notesJson"]) == ["Yerba Maté", "Rose"]


def test_cli_refuses_to_apply_to_a_remote_database_without_allow_remote(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@db.example.com:5432/prod")
    assert di.main(["validate", "--apply"]) == 2
    out = capsys.readouterr().out
    assert "refusing" in out and "pw" not in out
