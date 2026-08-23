"""Port of app/services/locationVerification.server.js -- a city is only ever "verified" through
a real geocoding result or a real match against the order-history city database, NEVER just
because the model accepted whatever text the customer typed. Closes the "Vice City" bug.
"""

from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrderHistory

_GEOCODE_TIMEOUT_SECONDS = 8.0


async def _http_get(url: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=_GEOCODE_TIMEOUT_SECONDS) as client:
        return await client.get(url)


async def _geocode_place(place_name: str) -> dict[str, Any]:
    url = f"https://geocoding-api.open-meteo.com/v1/search?count=5&name={quote(place_name)}"
    try:
        response = await _http_get(url)
    except Exception as err:
        return {"results": [], "error": str(err)}
    if not (200 <= response.status_code < 300):
        return {"results": []}
    data = response.json()
    results = data.get("results")
    return {"results": results if isinstance(results, list) else []}


_NO_MATCH = {"verified": False, "city": None, "country": None, "source": None, "needsClarification": False, "candidates": []}


async def verify_city(session: AsyncSession, city_text: str | None) -> dict[str, Any]:
    trimmed = (city_text or "").strip()
    if not trimmed:
        return dict(_NO_MATCH)

    # Fast path: a real city already present in order history, case-insensitive exact match. Takes
    # the first match rather than requiring a single distinct row -- the same real city is stored
    # under several different casings in the source data; any one of those rows names the same
    # real place, so ambiguity isn't a concern at this tier (only at the geocoding tier below,
    # where two DIFFERENT real places can share a name).
    history_match = await session.scalar(
        select(OrderHistory).where(func.lower(OrderHistory.city) == trimmed.lower()).limit(1)
    )
    if history_match:
        return {
            "verified": True,
            "city": history_match.city,
            "country": history_match.countryName or None,
            "source": "order_history",
            "needsClarification": False,
            "candidates": [],
        }

    # Real geocoding -- accepts a real city even with zero historical orders.
    geocode = await _geocode_place(trimmed)
    results = geocode["results"]
    if not results:
        return dict(_NO_MATCH)

    seen: dict[str, dict] = {}
    for r in results:
        key = f"{r.get('name')}|{r.get('country')}"
        seen.setdefault(key, r)
    distinct_candidates = list(seen.values())

    if len(distinct_candidates) > 1:
        return {
            "verified": False,
            "city": None,
            "country": None,
            "source": None,
            "needsClarification": True,
            "candidates": [
                {"city": c.get("name"), "country": c.get("country") or ""} for c in distinct_candidates[:5]
            ],
        }

    match = distinct_candidates[0]
    return {
        "verified": True,
        "city": match.get("name"),
        "country": match.get("country") or None,
        "source": "geocoding",
        "needsClarification": False,
        "candidates": [],
    }


async def fetch_current_weather(verified_city_name: str) -> dict[str, Any] | None:
    geocode = await _geocode_place(verified_city_name)
    results = geocode["results"]
    if not results:
        return None
    place = results[0]

    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={place['latitude']}&longitude={place['longitude']}"
        "&current=temperature_2m,weather_code,relative_humidity_2m&temperature_unit=fahrenheit"
    )
    try:
        response = await _http_get(url)
    except Exception:
        return None
    if not (200 <= response.status_code < 300):
        return None
    data = response.json()
    current = data.get("current")
    if not current:
        return None

    humidity = current.get("relative_humidity_2m")
    return {
        "tempF": round(current["temperature_2m"]),
        "weatherCode": current["weather_code"],
        "relativeHumidityPercent": round(humidity) if isinstance(humidity, (int, float)) else None,
    }
