"""Reference-data imports and maintenance (replaces the reference app's scripts/*.cjs importers).

    python -m scripts.data_import <command> [options]           # DRY RUN (default): nothing is kept
    python -m scripts.data_import <command> [options] --apply   # commit

Commands
    catalog        --file <Notes-Extraction-Separated-*.xlsx>   products, inspirations, Hybrid/Tribrid/Quadbrid
                   [--only products|inspirations|combinations]
    oil-skus       --file <...with-Oil-SKU update.xlsx>          OdooOilMapping (product -> Odoo oil SKU)
    notes          --notes-csv notes.csv --orders-csv order_history.csv   Note metadata, positions, families
    order-history  --file order_history.csv [--replace]         cleaned order history (authorized dataset only)
    summaries      [--prune-stale]                              regenerate ProductRegionSummary
    fix-note-encoding                                           repair stored note mojibake
    validate                                                    read-only duplicate/reference checks

Safety
  * Every command runs in ONE transaction. Without --apply it is rolled back after reporting the
    exact counts it would have produced (inserted / updated / unchanged / skipped, with reasons).
  * Nothing here drops or truncates a table. The only row deletions are: `order-history --replace`
    (the documented full-replace of that dataset, same as the reference seed) and
    `summaries --prune-stale` (derived rows no longer produced by the current order history).
  * --apply against a non-local database additionally requires --allow-remote. Validate on a
    disposable database first (docs/FEATURE_RESTORATION.md, "Data maintenance").
  * Identifiers and keys use the application's own functions: normalize_product_name,
    create_combination_key, and the reference app's customer-key HMAC. Note text always passes
    through fix_note_encoding, so imports cannot reintroduce mojibake.
  * Never prints the connection string, customer names or hashes; order-history skip reports show
    row numbers and reasons only.
"""

import argparse
import asyncio
import csv
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import zipfile
from collections import Counter, defaultdict
from urllib.parse import urlsplit
from xml.etree import ElementTree

import asyncpg

from app.db.ids import new_id
from app.fragrance.combination_key import create_combination_key
from app.fragrance.normalization import normalize_product_name
from scripts.note_encoding import MOJIBAKE, fix_note_encoding

logger = logging.getLogger("data_import")
BATCH = 500
_SKIP_SAMPLE = 25


class ImportAbort(Exception):
    pass


# ---------------------------------------------------------------------------
# Minimal .xlsx reader (stdlib only): cell values as SheetJS would return them for these sheets
# ---------------------------------------------------------------------------

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _col_index(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _number(text: str):
    value = float(text)
    return int(value) if value.is_integer() else value


def read_xlsx_sheet(path: str, sheet_name: str) -> list[list]:
    """Rows as lists (index = column), None for empty cells. Strings, numbers and booleans only."""
    with zipfile.ZipFile(path) as z:
        workbook = ElementTree.fromstring(z.read("xl/workbook.xml"))
        rels = ElementTree.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels}
        sheet = next((s for s in workbook.find("m:sheets", _NS) if s.get("name") == sheet_name), None)
        if sheet is None:
            raise ImportAbort(f'sheet "{sheet_name}" not found')
        target = targets[sheet.get(_REL)].lstrip("/")
        target = target if target.startswith("xl/") else "xl/" + target
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ElementTree.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))
        root = ElementTree.fromstring(z.read(target))
    rows: list[list] = []
    for row in root.iter(f"{{{_NS['m']}}}row"):
        values: dict[int, object] = {}
        for cell in row.findall("m:c", _NS):
            kind, v = cell.get("t"), cell.find("m:v", _NS)
            if kind == "inlineStr":
                value = "".join(t.text or "" for t in cell.iter(f"{{{_NS['m']}}}t"))
            elif v is None or v.text is None:
                continue
            elif kind == "s":
                value = shared[int(v.text)]
            elif kind in ("str", "e"):
                value = v.text
            elif kind == "b":
                value = v.text == "1"
            else:
                value = _number(v.text)
            values[_col_index(cell.get("r"))] = value
        index = int(row.get("r")) - 1
        while len(rows) < index:
            rows.append([])
        rows.append([values.get(i) for i in range(max(values) + 1)] if values else [])
    return rows


def _cell(row: list, i: int):
    return row[i] if i < len(row) else None


def _text(value) -> str | None:
    """A trimmed string cell, or None (non-strings are not text)."""
    return (value.strip() or None) if isinstance(value, str) else None


def _js_string(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

class Report:
    def __init__(self, command: str):
        self.command = command
        self.counts: Counter = Counter()
        self.skipped: Counter = Counter()
        self.samples: list[str] = []
        self.warnings: list[str] = []

    def skip(self, reason: str, detail: str) -> None:
        self.skipped[reason] += 1
        if len(self.samples) < _SKIP_SAMPLE:
            self.samples.append(f"{reason}: {detail}")

    def as_dict(self) -> dict:
        return {"command": self.command, "counts": dict(self.counts), "skipped": dict(self.skipped), "skippedSamples": self.samples, "warnings": self.warnings}


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------

async def import_products(conn, path: str, report: Report) -> None:
    rows = read_xlsx_sheet(path, "Notes-Extraction-Separated")
    header = [_js_string(h).strip() for h in rows[0]]
    col = {name: header.index(name) for name in ("Handle", "Title", "Notes", "PricePer5ml", "Collection") if name in header}
    if set(col) != {"Handle", "Title", "Notes", "PricePer5ml", "Collection"}:
        raise ImportAbort(f"unexpected product sheet header: {header}")
    by_title: dict[str, dict] = {}
    for number, row in enumerate(rows[1:], start=2):
        if not row:
            continue
        title = _text(_cell(row, col["Title"]))
        if not title:
            report.skip("title_missing_or_not_text", f"row {number}")
            continue
        notes_cell = _cell(row, col["Notes"])
        notes_raw = fix_note_encoding(notes_cell.strip() if isinstance(notes_cell, str) else "") or None
        if notes_raw and MOJIBAKE in notes_raw:
            report.warnings.append(f"row {number} ({title}): note text still contains unrecognised mojibake; stored as is")
        price = _cell(row, col["PricePer5ml"])
        normalized = normalize_product_name(title)
        if not normalized:
            report.skip("title_normalizes_to_empty", f"row {number}")
            continue
        if normalized in by_title:
            report.counts["duplicate_title_rows_last_wins"] += 1
        by_title[normalized] = {
            "handle": _js_string(_cell(row, col["Handle"])).strip() or None,
            "title": title,
            "normalizedTitle": normalized,
            "notesRaw": notes_raw,
            "notesJson": [n.strip() for n in notes_raw.split(",") if n.strip()] if notes_raw else [],
            "collection": _cell(row, col["Collection"]) or None,
            "pricePer5ml": float(price) if isinstance(price, (int, float)) and not isinstance(price, bool) else None,
        }
    existing = {r["normalizedTitle"]: r for r in await conn.fetch('SELECT "normalizedTitle", handle, title, "notesRaw", "notesJson", collection, "pricePer5ml" FROM "FragranceProduct"')}
    for item in by_title.values():
        before = existing.get(item["normalizedTitle"])
        if before is None:
            report.counts["products_inserted"] += 1
        elif (before["handle"], before["title"], before["notesRaw"], json.loads(before["notesJson"]) if before["notesJson"] else [], before["collection"], before["pricePer5ml"]) == (
                item["handle"], item["title"], item["notesRaw"], item["notesJson"], item["collection"], item["pricePer5ml"]):
            report.counts["products_unchanged"] += 1
            continue
        else:
            report.counts["products_updated"] += 1
        await conn.execute(
            '''INSERT INTO "FragranceProduct" (id, handle, title, "normalizedTitle", "notesRaw", "notesJson", collection, "pricePer5ml", "createdAt", "updatedAt")
               VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, now(), now())
               ON CONFLICT ("normalizedTitle") DO UPDATE SET handle = EXCLUDED.handle, title = EXCLUDED.title, "notesRaw" = EXCLUDED."notesRaw",
                 "notesJson" = EXCLUDED."notesJson", collection = EXCLUDED.collection, "pricePer5ml" = EXCLUDED."pricePer5ml", "updatedAt" = now()''',
            new_id(), item["handle"], item["title"], item["normalizedTitle"], item["notesRaw"], json.dumps(item["notesJson"]), item["collection"], item["pricePer5ml"],
        )
    stale = set(existing) - set(by_title)
    if stale:
        report.warnings.append(f"{len(stale)} existing product(s) are not in this file; they are kept (nothing is deleted)")


async def import_inspirations(conn, path: str, report: Report) -> None:
    rows = read_xlsx_sheet(path, "Inspirations")[2:]  # merged title row + header row
    products = await conn.fetch('SELECT id, handle, "normalizedTitle", "tagLine", "inspirationName", "inspirationBrand", "isSingleInspiration" FROM "FragranceProduct"')
    by_title = {p["normalizedTitle"]: p for p in products}
    by_handle = {p["handle"]: p for p in products if p["handle"]}
    updates: dict[str, tuple] = {}  # product id -> (row, product, values); a later row for the same product wins
    for number, row in enumerate(rows, start=3):
        if not row:
            continue
        handle, title, tag_line, name, brand = (_text(_cell(row, i)) for i in range(5))
        if not title:
            report.skip("title_missing", f"row {number}")
            continue
        product = by_title.get(normalize_product_name(title))
        if product is not None:
            report.counts["matched_by_title"] += 1
        else:
            product = by_handle.get(handle) if handle else None
            if product is None:
                report.skip("no_catalog_match", f"row {number} ({title})")
                continue
            report.counts["matched_by_handle"] += 1
        if product["id"] in updates:
            report.skip("inspiration_duplicate_for_product_last_wins", f"row {updates[product['id']][0]} superseded by row {number} ({title})")
        updates[product["id"]] = (number, product, (tag_line, name, brand))
    for _number, product, (tag_line, name, brand) in updates.values():
        if (product["tagLine"], product["inspirationName"], product["inspirationBrand"], product["isSingleInspiration"]) == (tag_line, name, brand, True):
            report.counts["inspirations_unchanged"] += 1
            continue
        await conn.execute(
            'UPDATE "FragranceProduct" SET "tagLine" = $2, "inspirationName" = $3, "inspirationBrand" = $4, "isSingleInspiration" = true, "updatedAt" = now() WHERE id = $1',
            product["id"], tag_line, name, brand,
        )
        report.counts["inspirations_updated"] += 1


COMBINATION_SHEETS = (("Hybrid", "HYBRID", 2), ("Tribrid", "TRIBRID", 3), ("Quadbrid", "QUADBRID", 4))


async def import_combinations(conn, path: str, report: Report) -> None:
    catalog = {r["normalizedTitle"] for r in await conn.fetch('SELECT "normalizedTitle" FROM "FragranceProduct"')}
    existing = {r["componentKey"]: r for r in await conn.fetch('SELECT "componentKey", "normalizedTitle", title, type, "componentProductsJson", "tagLine" FROM "ExistingCombination"')}
    key_owner = {k: r["normalizedTitle"] for k, r in existing.items()}
    for sheet, combo_type, size in COMBINATION_SHEETS:
        by_key: dict[str, dict] = {}
        for number, row in enumerate(read_xlsx_sheet(path, sheet)[2:], start=3):
            if not row:
                continue
            title = _text(_cell(row, 1))
            if not title:
                report.skip(f"{sheet.lower()}_title_missing", f"row {number}")
                continue
            # Component g is the "Dua Inspiration Name" column of group g (by position: the
            # Quadbrid header mislabels one of them).
            components = [_text(_cell(row, 3 + g * 3 + 1)) for g in range(size)]
            if any(c is None for c in components):
                report.skip(f"{sheet.lower()}_component_blank", f"row {number} ({title})")
                continue
            unresolved = [c for c in components if normalize_product_name(c) not in catalog]
            if unresolved:
                report.counts[f"{sheet.lower()}_rows_with_unresolved_components"] += 1  # imported anyway, as before
            key = create_combination_key(components)
            normalized = normalize_product_name(title)
            if key in key_owner and key_owner[key] != normalized:
                report.skip(f"{sheet.lower()}_component_key_collision", f"row {number} ({title})")
                continue
            key_owner[key] = normalized
            if key in by_key:
                report.counts[f"{sheet.lower()}_duplicate_rows_last_wins"] += 1
            by_key[key] = {"title": title, "normalizedTitle": normalized, "components": components, "tagLine": _text(_cell(row, 2))}
        for key, item in by_key.items():
            before = existing.get(key)
            if before is None:
                report.counts[f"{sheet.lower()}_inserted"] += 1
            elif (before["title"], before["normalizedTitle"], before["type"], json.loads(before["componentProductsJson"]), before["tagLine"]) == (item["title"], item["normalizedTitle"], combo_type, item["components"], item["tagLine"]):
                report.counts[f"{sheet.lower()}_unchanged"] += 1
                continue
            else:
                report.counts[f"{sheet.lower()}_updated"] += 1
            await conn.execute(
                '''INSERT INTO "ExistingCombination" (id, title, "normalizedTitle", type, "componentProductsJson", "componentKey", "tagLine", "createdAt")
                   VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, now())
                   ON CONFLICT ("componentKey") DO UPDATE SET title = EXCLUDED.title, "normalizedTitle" = EXCLUDED."normalizedTitle", type = EXCLUDED.type,
                     "componentProductsJson" = EXCLUDED."componentProductsJson", "tagLine" = EXCLUDED."tagLine"''',
                new_id(), item["title"], item["normalizedTitle"], combo_type, json.dumps(item["components"]), key, item["tagLine"],
            )


# ---------------------------------------------------------------------------
# oil SKUs
# ---------------------------------------------------------------------------

SKU_COLUMNS = (5, 6, 7, 8)  # F..I


async def import_oil_skus(conn, path: str, report: Report) -> None:
    rows = read_xlsx_sheet(path, "Notes-Extraction-Separated")[1:]
    products = await conn.fetch('SELECT id, handle, "normalizedTitle" FROM "FragranceProduct"')
    by_handle = {p["handle"]: p["id"] for p in products if p["handle"]}
    by_title = {p["normalizedTitle"]: p["id"] for p in products}
    existing = {r["fragranceProductId"]: r for r in await conn.fetch('SELECT "fragranceProductId", "odooSku", active FROM "OdooOilMapping"')}
    seen: dict[str, int] = {}
    for number, row in enumerate(rows, start=2):
        if not row:
            continue
        handle, title = _cell(row, 0), _cell(row, 1)
        if not handle or not title or not isinstance(title, str):
            report.skip("malformed_row", f"row {number}")
            continue
        skus = [_js_string(_cell(row, i)).strip() for i in SKU_COLUMNS if _cell(row, i) not in (None, "")]
        skus = [s for s in skus if s]
        if len(row) > SKU_COLUMNS[-1] + 1 and any(_cell(row, i) not in (None, "") for i in range(SKU_COLUMNS[-1] + 1, len(row))):
            report.warnings.append(f"row {number} ({title}): values beyond column I are ignored")
        if not skus:
            report.skip("no_sku", f"row {number} ({title})")
            continue
        if len(skus) > 1:
            report.skip("multi_component_combo", f"row {number} ({title})")  # combination rows are never mapped
            continue
        product_id = by_handle.get(_js_string(handle)) or by_title.get(normalize_product_name(title))
        if not product_id:
            report.skip("no_catalog_match", f"row {number} ({title})")
            continue
        if product_id in seen:
            report.counts["duplicate_product_rows_last_wins"] += 1
        seen[product_id] = number
        before = existing.get(product_id)
        if before is not None and before["odooSku"] == skus[0] and before["active"]:
            report.counts["mappings_unchanged"] += 1
            continue
        report.counts["mappings_updated" if before is not None else "mappings_inserted"] += 1
        await conn.execute(
            '''INSERT INTO "OdooOilMapping" (id, "fragranceProductId", "odooSku", active, "createdAt", "updatedAt") VALUES ($1, $2, $3, true, now(), now())
               ON CONFLICT ("fragranceProductId") DO UPDATE SET "odooSku" = EXCLUDED."odooSku", active = true, "updatedAt" = now()''',
            new_id(), product_id, skus[0],
        )
    # Expected, not an error: several products (editions) can share one physical oil.
    report.counts["skus_shared_by_several_products"] = await conn.fetchval('SELECT count(*) FROM (SELECT "odooSku" FROM "OdooOilMapping" GROUP BY 1 HAVING count(*) > 1) shared')


# ---------------------------------------------------------------------------
# notes + order history
# ---------------------------------------------------------------------------

_INT = re.compile(r"^\s*([+-]?\d+)")
_FLOAT = re.compile(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")


def js_parse_int(value) -> int:
    match = _INT.match(str(value or ""))
    return int(match.group(1)) if match else 0


def js_parse_float(value) -> float:
    match = _FLOAT.match(str(value or ""))
    return float(match.group(1)) if match else 0.0


_CLASSIFICATION_PREFIX = re.compile(r"^CLASSIFICATION:\s*", re.I)


def clean_classification(raw) -> str | None:
    return _CLASSIFICATION_PREFIX.sub("", raw or "").strip() or None


def note_position(top: int, middle: int, base: int) -> str:
    highest = max(top, middle, base)
    return "top" if highest == top else "middle" if highest == middle else "base"


def hash_customer_key(raw_name, salt: str) -> str | None:
    """Same keyed hash as the reference app (customerKeyHash.js): HMAC-SHA256 over the trimmed,
    lower-cased name. The name itself is never stored."""
    if not salt:
        raise ImportAbort("CUSTOMER_KEY_HASH_SALT is not set; refusing to ingest order history without it")
    if not isinstance(raw_name, str) or not raw_name.strip():
        return None
    return hmac.new(salt.encode(), raw_name.strip().lower().encode(), hashlib.sha256).hexdigest()


def _read_csv(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.DictReader(handle) if any((v or "").strip() for v in row.values())]


async def import_notes(conn, notes_csv: str, orders_csv: str, report: Report) -> None:
    tally: dict[str, Counter] = defaultdict(Counter)
    for row in _read_csv(orders_csv):
        classification = clean_classification(row.get("Classification"))
        if not classification:
            continue
        for note in (fix_note_encoding(n.strip()) for n in (row.get("Notes") or "").split(",")):
            if note:
                tally[note][classification] += 1

    def family(note: str) -> str | None:
        counts = tally.get(note)
        return max(counts.items(), key=lambda kv: kv[1])[0] if counts else None  # ties: first seen, as before

    existing = {r["name"]: r for r in await conn.fetch('SELECT name, position, density, family FROM "Note"')}
    for number, row in enumerate(_read_csv(notes_csv), start=2):
        name = fix_note_encoding((row.get("Notes") or "").strip())
        if not name:
            report.skip("name_missing", f"row {number}")
            continue
        position = note_position(js_parse_int(row.get("Top")), js_parse_int(row.get("Middle")), js_parse_int(row.get("Base")))
        density = js_parse_float(row.get("Average Density"))
        item = (position, density, family(name))
        before = existing.get(name)
        if before is not None and (before["position"], before["density"], before["family"]) == item:
            report.counts["notes_unchanged"] += 1
            continue
        report.counts["notes_updated" if before is not None else "notes_inserted"] += 1
        await conn.execute(
            '''INSERT INTO "Note" (id, name, position, density, family, "createdAt") VALUES ($1, $2, $3, $4, $5, now())
               ON CONFLICT (name) DO UPDATE SET position = EXCLUDED.position, density = EXCLUDED.density, family = EXCLUDED.family''',
            new_id(), name, *item,
        )


ORDER_COLUMNS = ("Order Date", "Updated Season", "Classification", "Notes", "City", "State Name", "Country Name", "Product Name", "Name")


async def import_order_history(conn, path: str, replace: bool, salt: str, report: Report) -> None:
    rows = _read_csv(path)
    if rows and not set(ORDER_COLUMNS) <= set(rows[0]):
        raise ImportAbort(f"order history file is missing columns: {sorted(set(ORDER_COLUMNS) - set(rows[0]))}")
    existing = await conn.fetchval('SELECT count(*) FROM "OrderHistory"')
    if existing and not replace:
        raise ImportAbort(f"OrderHistory already has {existing} rows; pass --replace to replace the dataset (one transaction), never appended twice")
    if existing:
        report.counts["rows_replaced"] = existing
        await conn.execute('DELETE FROM "OrderHistory"')

    def opt(value) -> str | None:
        return (value or "").strip() or None

    batch = []
    catalog = {r["normalizedTitle"] for r in await conn.fetch('SELECT "normalizedTitle" FROM "FragranceProduct"')}
    unmatched: set[str] = set()
    for number, row in enumerate(rows, start=2):
        product = opt(row.get("Product Name"))
        normalized = normalize_product_name(product) or None
        if normalized and normalized not in catalog:
            unmatched.add(normalized)
        customer_hash = hash_customer_key(row.get("Name"), salt)
        if customer_hash is None:
            report.counts["rows_without_customer_key"] += 1
        batch.append((
            new_id(), row.get("Order Date") or None, row.get("Updated Season") or None, clean_classification(row.get("Classification")),
            fix_note_encoding((row.get("Notes") or "").strip()), opt(row.get("City")), opt(row.get("State Name")), opt(row.get("Country Name")),
            product, normalized, customer_hash,
        ))
        if len(batch) >= 1000:
            await _insert_orders(conn, batch)
            batch = []
    if batch:
        await _insert_orders(conn, batch)
    report.counts["rows_inserted"] = len(rows)
    report.counts["distinct_products_not_in_catalog"] = len(unmatched)


async def _insert_orders(conn, batch: list[tuple]) -> None:
    await conn.executemany(
        '''INSERT INTO "OrderHistory" (id, "orderDate", season, classification, notes, city, "stateName", "countryName", "productName", "normalizedProductName", "customerKeyHash", "createdAt")
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now())''',
        batch,
    )


# ---------------------------------------------------------------------------
# summaries (port of build-region-summary.cjs; same aggregation SQL)
# ---------------------------------------------------------------------------

_SCOPE_SQL = """
INSERT INTO "ProductRegionSummary" (id, "normalizedProductName", scope, "scopeValue", "orderCount", "distinctCustomerCount", "repeatCustomerCount", "updatedAt")
SELECT gen_random_uuid()::text, "normalizedProductName", '{scope}', scope_value, SUM(cnt)::int, COUNT(*)::int, (COUNT(*) FILTER (WHERE cnt > 1))::int, now()
FROM (
  SELECT "normalizedProductName", "{column}" AS scope_value, "customerKeyHash", COUNT(*) AS cnt
  FROM "OrderHistory"
  WHERE "normalizedProductName" IS NOT NULL AND "{column}" IS NOT NULL AND "customerKeyHash" IS NOT NULL
  GROUP BY "normalizedProductName", "{column}", "customerKeyHash"
) customer_order_counts
GROUP BY "normalizedProductName", scope_value
ON CONFLICT ("normalizedProductName", scope, "scopeValue") DO UPDATE SET "orderCount" = EXCLUDED."orderCount",
  "distinctCustomerCount" = EXCLUDED."distinctCustomerCount", "repeatCustomerCount" = EXCLUDED."repeatCustomerCount", "updatedAt" = now()
"""
_CLASSIFICATION_SQL = """
INSERT INTO "ProductRegionSummary" (id, "normalizedProductName", scope, "scopeValue", "orderCount", "distinctCustomerCount", "repeatCustomerCount", "updatedAt")
SELECT gen_random_uuid()::text, "normalizedProductName", 'classification_global', classification, COUNT(*)::int, 0, 0, now()
FROM "OrderHistory"
WHERE "normalizedProductName" IS NOT NULL AND classification IS NOT NULL
GROUP BY "normalizedProductName", classification
ON CONFLICT ("normalizedProductName", scope, "scopeValue") DO UPDATE SET "orderCount" = EXCLUDED."orderCount",
  "distinctCustomerCount" = 0, "repeatCustomerCount" = 0, "updatedAt" = now()
"""


async def build_summaries(conn, prune_stale: bool, report: Report) -> None:
    for column, scope in (("countryName", "country"), ("stateName", "state"), ("season", "season")):
        status = await conn.execute(_SCOPE_SQL.format(column=column, scope=scope))
        report.counts[f"{scope}_rows"] = int(status.split()[-1])
    status = await conn.execute(_CLASSIFICATION_SQL)
    report.counts["classification_global_rows"] = int(status.split()[-1])
    # Every row produced above has updatedAt = now() (the transaction start), so anything older
    # was not produced by the current order history.
    stale = await conn.fetchval('SELECT count(*) FROM "ProductRegionSummary" WHERE "updatedAt" < now()::timestamp(3)')
    report.counts["stale_rows"] = stale
    if prune_stale and stale:
        await conn.execute('DELETE FROM "ProductRegionSummary" WHERE "updatedAt" < now()::timestamp(3)')
        report.counts["stale_rows_deleted"] = stale
    elif stale:
        report.warnings.append(f"{stale} summary row(s) were not produced by the current order history; kept (use --prune-stale to remove)")


# ---------------------------------------------------------------------------
# maintenance
# ---------------------------------------------------------------------------

async def repair_note_encoding(conn, report: Report) -> None:
    rows = await conn.fetch('SELECT id, title, "notesRaw" FROM "FragranceProduct" WHERE "notesRaw" LIKE $1', f"%{MOJIBAKE}%")
    for row in rows:
        fixed = fix_note_encoding(row["notesRaw"])
        if MOJIBAKE in fixed:
            report.skip("unrecognised_mojibake", row["title"])
            continue
        notes_json = [n.strip() for n in fixed.split(",") if n.strip()]
        await conn.execute('UPDATE "FragranceProduct" SET "notesRaw" = $2, "notesJson" = $3::jsonb, "updatedAt" = now() WHERE id = $1', row["id"], fixed, json.dumps(notes_json))
        report.counts["products_fixed"] += 1
    notes = await conn.fetch('SELECT id, name FROM "Note" WHERE name LIKE $1', f"%{MOJIBAKE}%")
    report.counts["note_rows_with_mojibake"] = len(notes)
    if notes:
        report.warnings.append("Note rows with mojibake are reported, not renamed: renaming could collide with an existing clean name. Re-import notes instead.")


EXPECTED_COMPONENTS = {"HYBRID": 2, "TRIBRID": 3, "QUADBRID": 4}
EXPECTED_SCOPES = {"country", "state", "season", "classification_global"}


async def validate(conn, report: Report) -> None:
    c = report.counts
    c["products"] = await conn.fetchval('SELECT count(*) FROM "FragranceProduct"')
    c["products_without_notes"] = await conn.fetchval(
        '''SELECT count(*) FROM "FragranceProduct" WHERE CASE WHEN jsonb_typeof("notesJson") = 'array' THEN jsonb_array_length("notesJson") = 0 ELSE true END''')
    c["products_with_mojibake"] = await conn.fetchval('SELECT count(*) FROM "FragranceProduct" WHERE "notesRaw" LIKE $1', f"%{MOJIBAKE}%")
    c["products_with_inspiration"] = await conn.fetchval('SELECT count(*) FROM "FragranceProduct" WHERE "isSingleInspiration"')
    catalog = {r["normalizedTitle"] for r in await conn.fetch('SELECT "normalizedTitle" FROM "FragranceProduct"')}
    combos = await conn.fetch('SELECT type, title, "componentKey", "componentProductsJson" FROM "ExistingCombination"')
    c["combinations"] = len(combos)
    for combo in combos:
        components = json.loads(combo["componentProductsJson"])
        if len(components) != EXPECTED_COMPONENTS.get(combo["type"]):
            report.skip("combination_wrong_component_count", combo["title"])
        if create_combination_key(components) != combo["componentKey"]:
            report.skip("combination_key_mismatch", combo["title"])
        if any(normalize_product_name(x) not in catalog for x in components):
            c["combinations_with_unresolved_components"] += 1
    c["oil_mappings"] = await conn.fetchval('SELECT count(*) FROM "OdooOilMapping"')
    c["oil_mappings_to_missing_products"] = await conn.fetchval('SELECT count(*) FROM "OdooOilMapping" m WHERE NOT EXISTS (SELECT 1 FROM "FragranceProduct" p WHERE p.id = m."fragranceProductId")')
    c["oil_mappings_inactive"] = await conn.fetchval('SELECT count(*) FROM "OdooOilMapping" WHERE NOT active')
    c["single_products_without_oil_mapping"] = await conn.fetchval(
        '''SELECT count(*) FROM "FragranceProduct" p WHERE COALESCE(p.collection, '') NOT IN ('Hybrid', 'Tribrid', 'Quadbrid')
           AND NOT EXISTS (SELECT 1 FROM "OdooOilMapping" m WHERE m."fragranceProductId" = p.id AND m.active)''')
    c["notes"] = await conn.fetchval('SELECT count(*) FROM "Note"')
    c["order_history_rows"] = await conn.fetchval('SELECT count(*) FROM "OrderHistory"')
    c["order_history_rows_without_customer_key"] = await conn.fetchval('SELECT count(*) FROM "OrderHistory" WHERE "customerKeyHash" IS NULL')
    c["order_history_products_not_in_catalog"] = await conn.fetchval(
        'SELECT count(DISTINCT "normalizedProductName") FROM "OrderHistory" o WHERE "normalizedProductName" IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "FragranceProduct" p WHERE p."normalizedTitle" = o."normalizedProductName")')
    c["summary_rows"] = await conn.fetchval('SELECT count(*) FROM "ProductRegionSummary"')
    scopes = {r["scope"] for r in await conn.fetch('SELECT DISTINCT scope FROM "ProductRegionSummary"')}
    if c["summary_rows"] and scopes != EXPECTED_SCOPES:
        report.warnings.append(f"summary scopes {sorted(scopes)} differ from {sorted(EXPECTED_SCOPES)}")
    if c["order_history_rows"] and not c["summary_rows"]:
        report.warnings.append("order history exists but no summaries: run `summaries`")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _is_local(url: str) -> bool:
    parts = urlsplit(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    return parts.hostname in (None, "localhost", "127.0.0.1", "::1") or "host=/" in (parts.query or "")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.data_import", description="Reference-data imports (dry run unless --apply).")
    parser.add_argument("command", choices=["catalog", "oil-skus", "notes", "order-history", "summaries", "fix-note-encoding", "validate"])
    parser.add_argument("--file")
    parser.add_argument("--only", choices=["products", "inspirations", "combinations"])
    parser.add_argument("--notes-csv")
    parser.add_argument("--orders-csv")
    parser.add_argument("--replace", action="store_true", help="order-history: replace the existing dataset")
    parser.add_argument("--prune-stale", action="store_true", help="summaries: delete rows the current order history no longer produces")
    parser.add_argument("--apply", action="store_true", help="commit (default is a dry run that rolls back)")
    parser.add_argument("--allow-remote", action="store_true", help="required with --apply when DATABASE_URL is not local")
    parser.add_argument("--report", help="also write the JSON report to this file")
    return parser


async def run(args, url: str) -> dict:
    report = Report(args.command)
    conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    try:
        transaction = conn.transaction(readonly=args.command == "validate")
        await transaction.start()
        try:
            if args.command == "catalog":
                if not args.file:
                    raise ImportAbort("--file is required")
                if args.only in (None, "products"):
                    await import_products(conn, args.file, report)
                if args.only in (None, "inspirations"):
                    await import_inspirations(conn, args.file, report)
                if args.only in (None, "combinations"):
                    await import_combinations(conn, args.file, report)
            elif args.command == "oil-skus":
                if not args.file:
                    raise ImportAbort("--file is required")
                await import_oil_skus(conn, args.file, report)
            elif args.command == "notes":
                if not args.notes_csv or not args.orders_csv:
                    raise ImportAbort("--notes-csv and --orders-csv are required")
                await import_notes(conn, args.notes_csv, args.orders_csv, report)
            elif args.command == "order-history":
                if not args.file:
                    raise ImportAbort("--file is required")
                await import_order_history(conn, args.file, args.replace, os.environ.get("CUSTOMER_KEY_HASH_SALT", ""), report)
            elif args.command == "summaries":
                await build_summaries(conn, args.prune_stale, report)
            elif args.command == "fix-note-encoding":
                await repair_note_encoding(conn, report)
            else:
                await validate(conn, report)
        except BaseException:
            await transaction.rollback()
            raise
        committed = bool(args.apply) and args.command != "validate"
        await (transaction.commit() if committed else transaction.rollback())
    finally:
        await conn.close()
    result = {**report.as_dict(), "applied": committed}
    logger.info("IMPORT_JOB %s", json.dumps({"command": args.command, "applied": committed, "counts": result["counts"], "skipped": result["skipped"]}))
    return result


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parser().parse_args(argv)
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL is not set")
        return 2
    if args.apply and not _is_local(url) and not args.allow_remote:
        print("refusing: --apply against a non-local database also needs --allow-remote (validate on a disposable database first)")
        return 2
    target = urlsplit(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    print(f"target database: {target.hostname}/{target.path.lstrip('/')} ({'APPLY' if args.apply else 'dry run'})")
    try:
        result = asyncio.run(run(args, url))
    except ImportAbort as err:
        print(f"aborted, nothing changed: {err}")
        return 1
    text = json.dumps(result, indent=2, ensure_ascii=False)
    print(text)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            handle.write(text)
    if not result["applied"] and args.command != "validate":
        print("dry run: rolled back. Re-run with --apply to commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
