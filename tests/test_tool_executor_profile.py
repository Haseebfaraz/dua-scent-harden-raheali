import time
import uuid

import httpx
import pytest
from sqlalchemy import delete

from app.ai.tool_executor import execute_fragrance_tool
from app.db.models import CustomerProfileState
from app.services import location_verification as lv
from app.services.customer_profile import get_customer_profile, save_customer_profile_fields

SHOP_DOMAIN = "test-shop.myshopify.com"


def _new_conversation_id(label: str) -> str:
    return f"pytest-{label}-{time.time()}-{uuid.uuid4().hex[:8]}"


def _ctx(conversation_id: str, customer_name=None, customer_email="test@example.com") -> dict:
    return {"conversationId": conversation_id, "customerName": customer_name, "customerEmail": customer_email, "shopDomain": SHOP_DOMAIN}


async def _cleanup(session, conversation_id: str):
    await session.execute(delete(CustomerProfileState).where(CustomerProfileState.conversationId == conversation_id))
    await session.commit()


# ---- name guard ----

@pytest.mark.asyncio
async def test_rejects_mood_filler_reply_as_name(db_session):
    conversation_id = _new_conversation_id("nameguard")
    try:
        result = await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "name", "value": "not having a great day"}', _ctx(conversation_id, customer_name=None))
        assert result["modelContent"].startswith("Error")
        profile = await get_customer_profile(db_session, conversation_id)
        assert profile["name"] is None
    finally:
        await _cleanup(db_session, conversation_id)


@pytest.mark.asyncio
async def test_rejects_other_mood_filler_replies(db_session):
    conversation_id = _new_conversation_id("nameguard2")
    try:
        for value in ["good, how about you", "just tired today", "not bad"]:
            result = await execute_fragrance_tool(db_session, "save_customer_profile_field", f'{{"field": "name", "value": "{value}"}}', _ctx(conversation_id, customer_name=None))
            assert result["modelContent"].startswith("Error")
    finally:
        await _cleanup(db_session, conversation_id)


@pytest.mark.asyncio
async def test_rejects_greeting_and_placeholder_nouns(db_session):
    conversation_id = _new_conversation_id("nameguard3")
    try:
        for value in ["hello", "hi", "hey", "user", "guest", "customer", "admin", "test"]:
            result = await execute_fragrance_tool(db_session, "save_customer_profile_field", f'{{"field": "name", "value": "{value}"}}', _ctx(conversation_id, customer_name=None))
            assert result["modelContent"].startswith("Error")
    finally:
        await _cleanup(db_session, conversation_id)


@pytest.mark.asyncio
async def test_accepts_real_name(db_session):
    conversation_id = _new_conversation_id("nameguard4")
    try:
        result = await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "name", "value": "Yamel"}', _ctx(conversation_id, customer_name=None))
        assert not result["modelContent"].startswith("Error")
        profile = await get_customer_profile(db_session, conversation_id)
        assert profile["name"] == "Yamel"
    finally:
        await _cleanup(db_session, conversation_id)


@pytest.mark.asyncio
async def test_accepts_real_two_word_name(db_session):
    conversation_id = _new_conversation_id("nameguard5")
    try:
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "name", "value": "Micheal Smith"}', _ctx(conversation_id, customer_name=None))
        profile = await get_customer_profile(db_session, conversation_id)
        assert profile["name"] == "Micheal Smith"
    finally:
        await _cleanup(db_session, conversation_id)


# ---- gift recipient ----

@pytest.mark.asyncio
async def test_saves_gift_recipient(db_session):
    conversation_id = _new_conversation_id("gift1")
    try:
        result = await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "giftRecipient", "value": "husband"}', _ctx(conversation_id))
        assert not result["modelContent"].startswith("Error")
        profile = await get_customer_profile(db_session, conversation_id)
        assert profile["giftRecipient"] == "husband"
    finally:
        await _cleanup(db_session, conversation_id)


@pytest.mark.asyncio
async def test_gift_recipient_defaults_to_none(db_session):
    conversation_id = _new_conversation_id("gift2")
    try:
        profile = await get_customer_profile(db_session, conversation_id)
        assert profile["giftRecipient"] is None
    finally:
        await _cleanup(db_session, conversation_id)


@pytest.mark.asyncio
async def test_other_fields_save_alongside_gift_recipient(db_session):
    conversation_id = _new_conversation_id("gift3")
    try:
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "giftRecipient", "value": "wife"}', _ctx(conversation_id))
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "likes", "value": ["Floral"]}', _ctx(conversation_id))
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "occasion", "value": "birthday"}', _ctx(conversation_id))
        profile = await get_customer_profile(db_session, conversation_id)
        assert profile["giftRecipient"] == "wife"
        assert profile["likes"] == ["Floral"]
        assert profile["occasion"] == "birthday"
    finally:
        await _cleanup(db_session, conversation_id)


# ---- season / weather ----

def _mock_geocode_response(results):
    return httpx.Response(200, json={"results": results})


def _mock_la_weather(monkeypatch, temp_f=75, weather_code=1, humidity=50):
    calls = []

    async def _get(url):
        calls.append(url)
        if len(calls) == 1:
            return _mock_geocode_response([{"name": "Los Angeles", "country": "United States", "latitude": 34.05, "longitude": -118.24}])
        return httpx.Response(200, json={"current": {"temperature_2m": temp_f, "weather_code": weather_code, "relative_humidity_2m": humidity}})

    monkeypatch.setattr(lv, "_http_get", _get)


@pytest.mark.asyncio
async def test_verify_location_autofetches_weather(db_session, monkeypatch):
    conversation_id = _new_conversation_id("season1")
    try:
        _mock_la_weather(monkeypatch)
        result = await execute_fragrance_tool(db_session, "verify_customer_location", '{"cityText": "Los Angeles"}', _ctx(conversation_id))

        assert "which season" not in result["modelContent"].lower()
        assert "adjusted accordingly" not in result["modelContent"].lower()
        assert "according to your weather" not in result["modelContent"].lower()

        profile = await get_customer_profile(db_session, conversation_id)
        assert profile["locationVerified"] is True
        assert profile["city"]
        assert profile["weatherDirection"] in ("hot", "warm", "mild", "cool", "cold", "humid", "rainy")
        assert profile["currentWeather"]["condition"]
    finally:
        await _cleanup(db_session, conversation_id)


@pytest.mark.asyncio
async def test_verify_location_rejects_fictional_city(db_session, monkeypatch):
    conversation_id = _new_conversation_id("season2")
    try:
        async def _get(url):
            return _mock_geocode_response([])

        monkeypatch.setattr(lv, "_http_get", _get)
        await execute_fragrance_tool(db_session, "verify_customer_location", '{"cityText": "Vice City"}', _ctx(conversation_id))
        profile = await get_customer_profile(db_session, conversation_id)
        assert profile["locationVerified"] is False
        assert profile["weatherDirection"] is None
    finally:
        await _cleanup(db_session, conversation_id)


@pytest.mark.asyncio
async def test_season_style_no_conflict_with_no_weather_yet(db_session):
    conversation_id = _new_conversation_id("season3")
    try:
        result = await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "requestedSeasonStyle", "value": "Winter"}', _ctx(conversation_id))
        assert "no real conflict" in result["modelContent"].lower()
        profile = await get_customer_profile(db_session, conversation_id)
        assert profile["requestedSeasonStyle"] == "Winter"
    finally:
        await _cleanup(db_session, conversation_id)


@pytest.mark.asyncio
async def test_season_conflict_keep_style(db_session, monkeypatch):
    conversation_id = _new_conversation_id("season4")
    try:
        _mock_la_weather(monkeypatch)
        await execute_fragrance_tool(db_session, "verify_customer_location", '{"cityText": "Los Angeles"}', _ctx(conversation_id))
        await save_customer_profile_fields(db_session, conversation_id, {"weatherDirection": "hot"})

        save_result = await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "requestedSeasonStyle", "value": "Winter"}', _ctx(conversation_id))
        assert "ask the customer once" in save_result["modelContent"].lower()

        resolve_result = await execute_fragrance_tool(db_session, "resolve_season_preference", '{"choice": "keep_style"}', _ctx(conversation_id))
        assert "keeping the requested winter style" in resolve_result["modelContent"].lower()
        profile = await get_customer_profile(db_session, conversation_id)
        assert profile["requestedSeasonStyle"] == "Winter"
        assert profile["seasonStyleConflictResolved"] is True
    finally:
        await _cleanup(db_session, conversation_id)


@pytest.mark.asyncio
async def test_season_conflict_use_weather_clears_style(db_session, monkeypatch):
    conversation_id = _new_conversation_id("season5")
    try:
        _mock_la_weather(monkeypatch)
        await execute_fragrance_tool(db_session, "verify_customer_location", '{"cityText": "Los Angeles"}', _ctx(conversation_id))
        await save_customer_profile_fields(db_session, conversation_id, {"weatherDirection": "hot", "requestedSeasonStyle": "Winter"})

        await execute_fragrance_tool(db_session, "resolve_season_preference", '{"choice": "use_weather"}', _ctx(conversation_id))
        profile = await get_customer_profile(db_session, conversation_id)
        assert profile["requestedSeasonStyle"] is None
        assert profile["seasonStyleConflictResolved"] is True
    finally:
        await _cleanup(db_session, conversation_id)


@pytest.mark.asyncio
async def test_analyze_candidates_never_requires_season(db_session, monkeypatch):
    conversation_id = _new_conversation_id("season6")
    try:
        _mock_la_weather(monkeypatch)
        await execute_fragrance_tool(db_session, "verify_customer_location", '{"cityText": "Los Angeles"}', _ctx(conversation_id))
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "likes", "value": ["Fruity"]}', _ctx(conversation_id))
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "dislikesAsked", "value": true}', _ctx(conversation_id))
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "occasionAsked", "value": true}', _ctx(conversation_id))
        result = await execute_fragrance_tool(db_session, "analyze_customer_product_candidates", "{}", _ctx(conversation_id))
        assert "missing required fields" not in result["modelContent"].lower()
    finally:
        await _cleanup(db_session, conversation_id)
