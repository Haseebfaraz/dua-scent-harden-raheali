"""Port of app/services/odooClient.server.js -- low-level HTTP wrapper around the custom Odoo
REST API, read-only. No DB, no Shopify calls, no recommendation logic (that lives in
odoo_inventory.py). Odoo must never be reachable from the browser/theme JS -- this module is
server-only by construction (never imported by anything client-facing).
"""

import json
import time
from typing import Any
from urllib.parse import quote

import httpx

from app.config import settings

# A candidate combination is checked before it's known to be the winner, and there's no cap on how
# long Odoo can take to answer -- this bounds the worst case per call so a slow/hung Odoo never
# blocks the chat turn indefinitely.
_REQUEST_TIMEOUT_SECONDS = 8.0


def _auth_headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if settings.odoo_inventory_api_key:
        headers["Authorization"] = f"Bearer {settings.odoo_inventory_api_key}"
    return headers


async def _get_json(url: str) -> dict[str, Any]:
    started_at = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=_auth_headers())
        body_text = response.text
        try:
            parsed = json.loads(body_text)
        except ValueError:
            # Malformed/non-JSON response (e.g. an HTML 404 page) -- never guess, surface as-is.
            parsed = None
        return {
            "ok": 200 <= response.status_code < 300,
            "status": response.status_code,
            "durationMs": (time.monotonic() - started_at) * 1000,
            "body": body_text,
            "json": parsed,
        }
    except Exception as error:
        return {
            "ok": False, "status": None,
            "durationMs": (time.monotonic() - started_at) * 1000,
            "error": str(error),
        }


NOT_CONFIGURED = "odoo integration is not configured"


def _configured_url(value: str) -> str | None:
    """An explicit https URL set by the operator, or nothing. Never a built-in default."""
    value = (value or "").strip()
    return value if value.lower().startswith("https://") else None


def inventory_integration_configured() -> bool:
    return _configured_url(settings.odoo_inventory_url) is not None


async def ping_odoo() -> dict[str, Any]:
    url = _configured_url(settings.odoo_ping_url)
    if url is None:
        return {"ok": False, "status": None, "durationMs": 0, "error": NOT_CONFIGURED, "configured": False}
    return await _get_json(url)


async def get_inventory_by_skus(skus: list[str]) -> dict[str, Any]:
    """Inventory lookup by SKU/Internal Reference -- the primary, approved production lookup key
    (never fuzzy product-name matching). Genuinely batched: multiple SKUs go in ONE request via a
    comma-separated `skus` param. Returns the raw HTTP/JSON result; normalization happens in
    odoo_inventory.py, not here.
    """
    if not skus:
        return {"ok": False, "status": None, "durationMs": 0, "error": "at least one sku is required."}
    base = _configured_url(settings.odoo_inventory_url)
    if base is None:
        # Phase 5A: missing configuration makes ZERO requests.
        return {"ok": False, "status": None, "durationMs": 0, "error": NOT_CONFIGURED, "configured": False}
    return await _get_json(f"{base}?skus={quote(','.join(skus))}")
