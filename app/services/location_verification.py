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


_NO_MATCH = {
    "verified": False, "city": None, "stateRegion": None, "country": None,
    "latitude": None, "longitude": None, "source": None, "needsClarification": False, "candidates": [],
}


def _select_confident_match(candidates: list[dict]) -> dict | None:
    """Picks the one candidate location should be treated as resolved, or None when the
    ambiguity is real enough to be worth asking about.

    Open-Meteo's geocoding API already returns results in its own relevance order -- for a
    well-known city (Liverpool, Paris, New York, Dubai, Karachi...) the first result is
    essentially always the intended place, with any same-named duplicates being minor towns far
    behind it. `population`, when the API supplies it, is a concrete way to confirm that: the top
    result is confident either because no other candidate has comparable population data to
    rival it, or because it clearly outweighs whatever rival exists. Only genuinely close
    population figures (a real contender, not a namesake village) fall through to asking the
    customer.
    """
    if len(candidates) == 1:
        return candidates[0]

    top = candidates[0]
    top_population = top.get("population") or 0
    if top_population <= 0:
        # No population data to compare with -- trust the geocoder's own top-ranked relevance
        # match rather than treating every same-named place as an even toss-up.
        return top

    rivals = [c for c in candidates[1:] if (c.get("population") or 0) >= top_population * 0.5]
    return top if not rivals else None


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
            "stateRegion": history_match.stateName or None,
            "country": history_match.countryName or None,
            "latitude": None,
            "longitude": None,
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

    match = _select_confident_match(distinct_candidates)
    if match is None:
        return {
            "verified": False,
            "city": None,
            "stateRegion": None,
            "country": None,
            "latitude": None,
            "longitude": None,
            "source": None,
            "needsClarification": True,
            "candidates": [
                {"city": c.get("name"), "country": c.get("country") or ""} for c in distinct_candidates[:5]
            ],
        }

    return {
        "verified": True,
        "city": match.get("name"),
        "stateRegion": match.get("admin1") or None,
        "country": match.get("country") or None,
        "latitude": match.get("latitude"),
        "longitude": match.get("longitude"),
        "source": "geocoding",
        "needsClarification": False,
        "candidates": [],
    }


async def fetch_current_weather(
    verified_city_name: str, latitude: float | None = None, longitude: float | None = None
) -> dict[str, Any] | None:
    # verify_city's geocoding tier already resolves real latitude/longitude for the exact place it
    # just confirmed -- reuse that instead of re-geocoding by name a second time (and risking a
    # different top match than the one actually verified). Only re-geocodes when the caller
    # genuinely doesn't have coordinates yet (e.g. an order-history-only match).
    if latitude is None or longitude is None:
        geocode = await _geocode_place(verified_city_name)
        results = geocode["results"]
        if not results:
            return None
        place = results[0]
        latitude, longitude = place["latitude"], place["longitude"]

    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}"
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
