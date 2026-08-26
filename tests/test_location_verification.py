import httpx
import pytest

from app.services import location_verification as lv


def _mock_geocode_response(results):
    return httpx.Response(200, json={"results": results})


@pytest.mark.asyncio
async def test_verifies_real_city_via_order_history_fast_path(db_session, monkeypatch):
    async def _should_not_be_called(url):
        raise AssertionError("no network call expected for an order-history hit")

    monkeypatch.setattr(lv, "_http_get", _should_not_be_called)
    result = await lv.verify_city(db_session, "Los Angeles")
    assert result["verified"] is True
    assert result["source"] == "order_history"
    assert result["country"]


@pytest.mark.asyncio
async def test_rejects_empty_string_without_network_call(db_session, monkeypatch):
    called = False

    async def _tracked(url):
        nonlocal called
        called = True
        return _mock_geocode_response([])

    monkeypatch.setattr(lv, "_http_get", _tracked)
    result = await lv.verify_city(db_session, "")
    assert result == {
        "verified": False, "city": None, "stateRegion": None, "country": None,
        "latitude": None, "longitude": None, "source": None,
        "needsClarification": False, "candidates": [],
    }
    assert called is False


@pytest.mark.asyncio
async def test_rejects_fictional_city_vice_city_bug(db_session, monkeypatch):
    monkeypatch.setattr(lv, "_http_get", lambda url: _mock_geocode_response([]))
    result = await lv.verify_city(db_session, "Vice City")
    assert result["verified"] is False


@pytest.mark.asyncio
async def test_accepts_real_city_with_zero_order_history(db_session, monkeypatch):
    async def _get(url):
        return _mock_geocode_response([{"name": "Nonexistentville", "country": "Iceland", "latitude": 64.15, "longitude": -21.94}])

    monkeypatch.setattr(lv, "_http_get", _get)
    result = await lv.verify_city(db_session, "Nonexistentville")
    assert result["verified"] is True
    assert result["source"] == "geocoding"
    assert result["city"] == "Nonexistentville"


@pytest.mark.asyncio
async def test_prefers_top_geocoding_result_when_no_population_data(db_session, monkeypatch):
    # Real place names can collide (a namesake town), but Open-Meteo already ranks its own top
    # result as the intended place -- without population data to weigh a rival, auto-resolve to
    # it rather than asking the customer to disambiguate every shared city name.
    async def _get(url):
        return _mock_geocode_response([
            {"name": "Paris", "country": "France", "latitude": 48.85, "longitude": 2.35},
            {"name": "Paris", "country": "United States", "latitude": 33.66, "longitude": -95.55},
        ])

    monkeypatch.setattr(lv, "_http_get", _get)
    result = await lv.verify_city(db_session, "Paris-Not-In-Order-History-Test")
    assert result["verified"] is True
    assert result["needsClarification"] is False
    assert result["city"] == "Paris"
    assert result["country"] == "France"


@pytest.mark.asyncio
async def test_flags_genuinely_comparable_population_candidates_as_needing_clarification(db_session, monkeypatch):
    async def _get(url):
        return _mock_geocode_response([
            {"name": "Springfield", "country": "United States", "latitude": 39.78, "longitude": -89.65, "population": 114000},
            {"name": "Springfield", "country": "Canada", "latitude": 45.19, "longitude": -66.98, "population": 92000},
        ])

    monkeypatch.setattr(lv, "_http_get", _get)
    result = await lv.verify_city(db_session, "Springfield-Not-In-Order-History-Test")
    assert result["needsClarification"] is True
    assert result["verified"] is False
    assert len(result["candidates"]) == 2


@pytest.mark.asyncio
async def test_timeout_treated_as_no_match(db_session, monkeypatch):
    async def _get(url):
        raise httpx.TimeoutException("The operation was aborted")

    monkeypatch.setattr(lv, "_http_get", _get)
    result = await lv.verify_city(db_session, "Some-Timeout-City-Test")
    assert result["verified"] is False


@pytest.mark.asyncio
async def test_malformed_response_treated_as_no_match(db_session, monkeypatch):
    async def _get(url):
        return httpx.Response(200, json={"notResults": "oops"})

    monkeypatch.setattr(lv, "_http_get", _get)
    result = await lv.verify_city(db_session, "Some-Malformed-Response-City-Test")
    assert result["verified"] is False


@pytest.mark.asyncio
async def test_network_error_treated_as_no_match(db_session, monkeypatch):
    async def _get(url):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(lv, "_http_get", _get)
    result = await lv.verify_city(db_session, "Some-Network-Error-City-Test")
    assert result["verified"] is False


@pytest.mark.asyncio
async def test_non_ok_response_treated_as_no_match(db_session, monkeypatch):
    async def _get(url):
        return httpx.Response(500)

    monkeypatch.setattr(lv, "_http_get", _get)
    result = await lv.verify_city(db_session, "Some-500-City-Test")
    assert result["verified"] is False


@pytest.mark.asyncio
async def test_fetch_current_weather_success(monkeypatch):
    calls = []

    async def _get(url):
        calls.append(url)
        if len(calls) == 1:
            return _mock_geocode_response([{"name": "Miami", "country": "United States", "latitude": 25.77, "longitude": -80.19}])
        return httpx.Response(200, json={"current": {"temperature_2m": 88, "weather_code": 0, "relative_humidity_2m": 70}})

    monkeypatch.setattr(lv, "_http_get", _get)
    result = await lv.fetch_current_weather("Miami")
    assert result == {"tempF": 88, "weatherCode": 0, "relativeHumidityPercent": 70}


@pytest.mark.asyncio
async def test_fetch_current_weather_null_on_no_geocode_results(monkeypatch):
    async def _get(url):
        return _mock_geocode_response([])

    monkeypatch.setattr(lv, "_http_get", _get)
    result = await lv.fetch_current_weather("Nowhere-At-All-Test")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_current_weather_null_on_forecast_network_error(monkeypatch):
    calls = []

    async def _get(url):
        calls.append(url)
        if len(calls) == 1:
            return _mock_geocode_response([{"name": "Miami", "country": "United States", "latitude": 25.77, "longitude": -80.19}])
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(lv, "_http_get", _get)
    result = await lv.fetch_current_weather("Miami")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_current_weather_null_on_malformed_forecast(monkeypatch):
    calls = []

    async def _get(url):
        calls.append(url)
        if len(calls) == 1:
            return _mock_geocode_response([{"name": "Miami", "country": "United States", "latitude": 25.77, "longitude": -80.19}])
        return httpx.Response(200, json={"notCurrent": {}})

    monkeypatch.setattr(lv, "_http_get", _get)
    result = await lv.fetch_current_weather("Miami")
    assert result is None


def _mock_geocode(monkeypatch, name, country, admin1, latitude, longitude, population=None):
    candidate = {"name": name, "country": country, "admin1": admin1, "latitude": latitude, "longitude": longitude}
    if population is not None:
        candidate["population"] = population

    async def _get(url):
        return _mock_geocode_response([candidate])

    monkeypatch.setattr(lv, "_http_get", _get)


@pytest.mark.asyncio
async def test_liverpool_auto_resolves_city_region_and_country(db_session, monkeypatch):
    _mock_geocode(monkeypatch, "Liverpool", "United Kingdom", "England", 53.41, -2.98)
    result = await lv.verify_city(db_session, "Liverpool-Not-In-Order-History-Test")
    assert result["verified"] is True
    assert result["needsClarification"] is False
    assert result["city"] == "Liverpool"
    assert result["stateRegion"] == "England"
    assert result["country"] == "United Kingdom"


@pytest.mark.asyncio
async def test_liverpool_uk_normalizes_and_verifies_directly(db_session, monkeypatch):
    _mock_geocode(monkeypatch, "Liverpool", "United Kingdom", "England", 53.41, -2.98)
    result = await lv.verify_city(db_session, "Liverpool, UK")
    assert result["verified"] is True
    assert result["needsClarification"] is False
    assert result["city"] == "Liverpool"
    assert result["stateRegion"] == "England"
    assert result["country"] == "United Kingdom"


@pytest.mark.asyncio
async def test_new_york_auto_resolves_state_and_country(db_session, monkeypatch):
    _mock_geocode(monkeypatch, "New York", "United States", "New York", 40.71, -74.01)
    result = await lv.verify_city(db_session, "New-York-Not-In-Order-History-Test")
    assert result["verified"] is True
    assert result["needsClarification"] is False
    assert result["city"] == "New York"
    assert result["stateRegion"] == "New York"
    assert result["country"] == "United States"


@pytest.mark.asyncio
async def test_paris_auto_resolves_to_france(db_session, monkeypatch):
    _mock_geocode(monkeypatch, "Paris", "France", "Île-de-France", 48.85, 2.35)
    result = await lv.verify_city(db_session, "Paris-Auto-Resolve-Test")
    assert result["verified"] is True
    assert result["needsClarification"] is False
    assert result["city"] == "Paris"
    assert result["country"] == "France"


@pytest.mark.asyncio
async def test_dubai_auto_resolves_to_united_arab_emirates(db_session, monkeypatch):
    _mock_geocode(monkeypatch, "Dubai", "United Arab Emirates", "Dubai", 25.2, 55.27)
    result = await lv.verify_city(db_session, "Dubai-Not-In-Order-History-Test")
    assert result["verified"] is True
    assert result["needsClarification"] is False
    assert result["city"] == "Dubai"
    assert result["country"] == "United Arab Emirates"


@pytest.mark.asyncio
async def test_karachi_auto_resolves_to_pakistan(db_session, monkeypatch):
    _mock_geocode(monkeypatch, "Karachi", "Pakistan", "Sindh", 24.86, 67.0)
    result = await lv.verify_city(db_session, "Karachi-Not-In-Order-History-Test")
    assert result["verified"] is True
    assert result["needsClarification"] is False
    assert result["city"] == "Karachi"
    assert result["country"] == "Pakistan"


@pytest.mark.asyncio
async def test_invalid_location_only_then_allows_one_clarification(db_session, monkeypatch):
    async def _get(url):
        return _mock_geocode_response([])

    monkeypatch.setattr(lv, "_http_get", _get)
    result = await lv.verify_city(db_session, "Xyzzyplorp-Not-A-Real-Place")
    assert result["verified"] is False
    assert result["needsClarification"] is False
