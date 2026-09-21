"""Port of app/services/odooInventory.server.js -- normalizes raw Odoo API responses into ml and
a stock status, and resolves the DUA product -> Odoo SKU mapping. Hides both from recommendation
logic, which should only ever see clean {availableOilMl, stockStatus, ...} facts.

Real confirmed response shape: {"success": true, "products": [{"name", "default_code", "on_hand_qty"}]}.
Two real limitations this works around rather than papers over:
  - No reserved/available split -- only on_hand_qty, treated directly as the usable quantity.
  - No unit of measure field -- ASSUMED to already be ml (the production unit for fragrance oil).
A requested SKU absent from the returned `products` array is SKU_NOT_FOUND, matched by
`default_code` (Odoo's field name for what this app calls a SKU).
"""

import json
import logging
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import FragranceProduct, OdooOilMapping
from app.db.time import utcnow
from app.fragrance.formulas import build_production_formula, compute_component_capacity, compute_feasibility
from app.fragrance.normalization import normalize_product_name
from app.integrations.odoo_client import get_inventory_by_skus

logger = logging.getLogger(__name__)

# Buildable-bottle-count thresholds, not arbitrary raw ml -- a status is only meaningful relative
# to how many bottles it can actually produce.
_STOCK_STATUS_THRESHOLDS = {"IN_STOCK": 21, "LOW_STOCK": 6, "CRITICAL": 1}


def classify_stock_status(maximum_buildable_bottles: int | None) -> str:
    if maximum_buildable_bottles is None:
        return "UNKNOWN"
    if maximum_buildable_bottles >= _STOCK_STATUS_THRESHOLDS["IN_STOCK"]:
        return "IN_STOCK"
    if maximum_buildable_bottles >= _STOCK_STATUS_THRESHOLDS["LOW_STOCK"]:
        return "LOW_STOCK"
    if maximum_buildable_bottles >= _STOCK_STATUS_THRESHOLDS["CRITICAL"]:
        return "CRITICAL"
    return "OUT_OF_STOCK"


# ponytail: a short-lived in-memory cache, keyed by normalized title, exactly matching the JS
# module's own scope/lifetime. Global to the process (not per-session) -- same anchor/support
# product recurs across many ranked candidates within a short window; never indefinite.
_inventory_cache: dict[str, dict[str, Any]] = {}


def _get_cached(key: str) -> dict | None:
    entry = _inventory_cache.get(key)
    if not entry or entry["expiresAt"] < time.monotonic():
        return None
    return entry["result"]


def _set_cached(key: str, result: dict) -> None:
    ttl = settings.odoo_inventory_cache_ttl_seconds
    _inventory_cache[key] = {"expiresAt": time.monotonic() + ttl, "result": result}


def clear_odoo_inventory_cache_for_testing() -> None:
    _inventory_cache.clear()


async def _resolve_mappings_to_inventory(session: AsyncSession, mappings_by_fragrance_product_id: dict[str, OdooOilMapping]) -> dict[str, dict]:
    """Given fragranceProductId -> active mapping row, makes ONE real Odoo call for all of their
    SKUs at once and returns fragranceProductId -> normalized inventory result.
    """
    checked_at = utcnow().isoformat()
    mapping_entries = list(mappings_by_fragrance_product_id.items())
    skus = [mapping.odooSku for _, mapping in mapping_entries]
    response = await get_inventory_by_skus(skus)
    results_by_fragrance_product_id: dict[str, dict] = {}

    if not response["ok"] or not (response.get("json") or {}).get("success"):
        for fragrance_product_id, mapping in mapping_entries:
            results_by_fragrance_product_id[fragrance_product_id] = {
                "fragranceProductId": fragrance_product_id, "odooSku": mapping.odooSku, "found": False,
                "availableOilMl": None, "unit": None, "mappingStatus": "LOOKUP_FAILED", "checkedAt": checked_at,
            }
        return results_by_fragrance_product_id

    products_by_sku = {p.get("default_code"): p for p in (response["json"].get("products") or [])}
    for fragrance_product_id, mapping in mapping_entries:
        product = products_by_sku.get(mapping.odooSku)
        if not product:
            results_by_fragrance_product_id[fragrance_product_id] = {
                "fragranceProductId": fragrance_product_id, "odooSku": mapping.odooSku, "found": False,
                "availableOilMl": None, "unit": None, "mappingStatus": "SKU_NOT_FOUND", "checkedAt": checked_at,
            }
            continue
        on_hand_qty = product.get("on_hand_qty")
        results_by_fragrance_product_id[fragrance_product_id] = {
            "fragranceProductId": fragrance_product_id, "odooSku": mapping.odooSku, "found": True,
            "availableOilMl": on_hand_qty if isinstance(on_hand_qty, (int, float)) else None,
            "unit": "ml",  # assumed -- the real API returns no UoM field
            "odooProductId": None, "name": product.get("name"),
            "mappingStatus": "CONNECTED", "checkedAt": checked_at,
        }
    return results_by_fragrance_product_id


async def get_oil_inventory_for_product(session: AsyncSession, fragrance_product_id: str) -> dict:
    """Bypasses the cache -- intended for a deliberate single fresh check (Save Build/Add to
    Cart), not for ranking many candidates.
    """
    checked_at = utcnow().isoformat()
    mapping = await session.scalar(select(OdooOilMapping).where(OdooOilMapping.fragranceProductId == fragrance_product_id))
    if not mapping or not mapping.active:
        return {"fragranceProductId": fragrance_product_id, "odooSku": None, "found": False, "availableOilMl": None, "unit": None, "mappingStatus": "MISSING", "checkedAt": checked_at}
    resolved = await _resolve_mappings_to_inventory(session, {fragrance_product_id: mapping})
    return resolved[fragrance_product_id]


async def resolve_odoo_skus_for_titles(session: AsyncSession, product_titles: list[str]) -> dict[str, dict | None]:
    """Mapping resolution only -- no Odoo call, no cache read/write."""
    unique_titles = list(dict.fromkeys(product_titles))
    result: dict[str, dict | None] = {}
    for title in unique_titles:
        product = await session.scalar(select(FragranceProduct).where(FragranceProduct.normalizedTitle == normalize_product_name(title)))
        if not product:
            result[title] = None
            continue
        mapping = await session.scalar(select(OdooOilMapping).where(OdooOilMapping.fragranceProductId == product.id))
        result[title] = {"fragranceProductId": product.id, "odooSku": mapping.odooSku} if mapping and mapping.active else None
    return result


async def get_oil_inventory_for_product_titles(session: AsyncSession, product_titles: list[str]) -> dict[str, Any]:
    """Batch-shaped entry point for checking one candidate's 2-4 real components at once -- makes
    AT MOST one genuine Odoo HTTP request total.
    """
    unique_titles = list(dict.fromkeys(product_titles))
    results: dict[str, dict] = {}
    needs_lookup = []  # {title, key, fragranceProductId, mapping}

    for title in unique_titles:
        key = normalize_product_name(title)
        cached = _get_cached(key)
        if cached:
            results[title] = cached
            continue

        checked_at = utcnow().isoformat()
        product = await session.scalar(select(FragranceProduct).where(FragranceProduct.normalizedTitle == key))
        if not product:
            result = {"fragranceProductId": None, "odooSku": None, "found": False, "availableOilMl": None, "unit": None, "mappingStatus": "MISSING", "checkedAt": checked_at}
            _set_cached(key, result)
            results[title] = result
            continue

        mapping = await session.scalar(select(OdooOilMapping).where(OdooOilMapping.fragranceProductId == product.id))
        if not mapping or not mapping.active:
            result = {"fragranceProductId": product.id, "odooSku": None, "found": False, "availableOilMl": None, "unit": None, "mappingStatus": "MISSING", "checkedAt": checked_at}
            _set_cached(key, result)
            results[title] = result
            continue

        needs_lookup.append({"title": title, "key": key, "fragranceProductId": product.id, "mapping": mapping})

    skus_queried = [n["mapping"].odooSku for n in needs_lookup]
    if needs_lookup:
        mappings_by_fragrance_product_id = {n["fragranceProductId"]: n["mapping"] for n in needs_lookup}
        resolved = await _resolve_mappings_to_inventory(session, mappings_by_fragrance_product_id)
        for n in needs_lookup:
            result = resolved[n["fragranceProductId"]]
            _set_cached(n["key"], result)
            results[n["title"]] = result

    return {"results": results, "requestCount": 1 if needs_lookup else 0, "skusQueried": skus_queried}


# ============================================================
# Odoo manufacturing feasibility gate (ported from fragranceAgentTools.server.js's
# evaluateCandidateInventory -- inventory-feasibility logic, kept alongside the rest of the Odoo
# integration rather than the AI tool-orchestration layer).
# ============================================================


async def evaluate_candidate_inventory(session: AsyncSession, candidate: dict[str, Any], candidate_index: int | None = None) -> dict[str, Any]:
    """The FINAL check, only for the one ranked candidate currently being considered.

    Fallback/WARN semantics: only a CONFIRMED "not enough" answer (mappingStatus CONNECTED and
    availableOilMl < requiredOilMl) makes buildable=false. Missing mapping / SKU not found /
    lookup failure never reject -- but inventoryValidated is false for ALL of those.
    """
    started_at = time.monotonic()
    try:
        formula = build_production_formula([
            {"productTitle": r["productTitle"], "ratioPercent": r["ratioPercent"]}
            for r in (candidate.get("recommendedRatio") or [])
        ])
        titles = [c["productTitle"] for c in formula["components"]]

        sku_map = await resolve_odoo_skus_for_titles(session, titles)
        # Phase 6: counts only. Source product titles, item codes and quantities are private
        # business data; the durable detail lives in RecommendationInventorySnapshot, not in logs.
        logger.info("ODOO_INVENTORY_REQUEST %s", json.dumps({
            "recommendationId": candidate.get("recommendationId"), "candidateIndex": candidate_index,
            "componentCount": len(titles), "mappedCount": sum(1 for t in titles if sku_map.get(t)),
        }))

        lookup = await get_oil_inventory_for_product_titles(session, titles)
        results, request_count, skus_queried = lookup["results"], lookup["requestCount"], lookup["skusQueried"]

        components = []
        for c in formula["components"]:
            inventory = results.get(c["productTitle"]) or {"mappingStatus": "MISSING", "odooSku": None, "fragranceProductId": None}
            on_hand_qty = inventory["availableOilMl"] if inventory["mappingStatus"] == "CONNECTED" else None
            components.append({
                "fragranceProductId": inventory.get("fragranceProductId"),
                "productTitle": c["productTitle"],
                "odooSku": inventory.get("odooSku"),
                "ratioPercent": c["ratioPercent"],
                "requiredOilMl": c["requiredOilMl"],
                "onHandQty": on_hand_qty,
                "mappingStatus": inventory["mappingStatus"],
                "sufficient": (on_hand_qty >= c["requiredOilMl"]) if on_hand_qty is not None else None,
                "maxBuildableBottlesForComponent": compute_component_capacity(on_hand_qty, c["requiredOilMl"]) if on_hand_qty is not None else None,
            })

        lookup_failed = [c for c in components if c["mappingStatus"] == "LOOKUP_FAILED"]
        if lookup_failed:
            logger.info("ODOO_INVENTORY_LOOKUP_FAILED %s", json.dumps({"recommendationId": candidate.get("recommendationId"), "componentCount": len(lookup_failed)}))

        known_components = [c for c in components if c["mappingStatus"] == "CONNECTED"]
        confirmed_insufficient = bool(known_components) and not compute_feasibility([
            {"productTitle": c["productTitle"], "requiredOilMl": c["requiredOilMl"], "availableOilMl": c["onHandQty"]}
            for c in known_components
        ])["buildable"]
        inventory_validated = len(known_components) == len(components) and len(components) > 0
        duration_ms = (time.monotonic() - started_at) * 1000
        status = "lookup_failed" if lookup_failed else "ok"

        logger.info("ODOO_INVENTORY_RESPONSE %s", json.dumps({
            "recommendationId": candidate.get("recommendationId"), "candidateIndex": candidate_index,
            "status": status, "inventoryValidated": inventory_validated, "durationMs": duration_ms,
            "componentCount": len(components), "sufficientCount": sum(1 for c in components if c["sufficient"]),
        }))

        limiting = None
        for c in known_components:
            if limiting is None or c["maxBuildableBottlesForComponent"] < limiting["maxBuildableBottlesForComponent"]:
                limiting = c

        return {
            "buildable": not confirmed_insufficient,
            "inventoryValidated": inventory_validated,
            "components": components,
            "requestCount": request_count,
            "skusQueried": skus_queried,
            "durationMs": duration_ms,
            "status": status,
            "oilTotalMl": formula["oilTotalMl"],
            "alcoholMl": formula["alcoholMl"],
            "maxBuildableBottles": limiting["maxBuildableBottlesForComponent"] if limiting else None,
            "limitingSku": limiting["odooSku"] if limiting else None,
        }
    except Exception:
        # Malformed ratios are already caught by shapeValid in the auto-confirm eligibility gate --
        # never let a formula-building error here reject a candidate for the wrong reason.
        return {
            "buildable": True, "inventoryValidated": False, "components": [], "requestCount": 0, "skusQueried": [],
            "durationMs": (time.monotonic() - started_at) * 1000, "status": "ok", "oilTotalMl": None, "alcoholMl": None,
            "maxBuildableBottles": None, "limitingSku": None,
        }
