import time
import uuid

import pytest
from sqlalchemy import delete, select

from app.ai.preview_url import build_preview_url
from app.db.models import CustomerProfileState, FragranceRecommendation
from app.services.legacy_preview_recovery import resolve_legacy_preview_short_circuit
from app.services.recommendation_confirmation import save_recommendation

# Phase 7: these tests need a product / hybrid / order-history row to exist, not the real catalog.
pytestmark = pytest.mark.usefixtures("synthetic_catalog")

SHOP_DOMAIN = "test-shop.myshopify.com"

_PAIR_A = {"first": {"title": "The Opera", "notes": ["Rose", "Fruity Notes", "Ambergris", "Leather", "Nutmeg", "Cedar", "Vanilla", "Musk"]}, "second": {"title": "Water of Arabia", "notes": ["Mandarin", "Bergamot", "Blackcurrant", "Green Tea", "Sandalwood"]}}
_PAIR_B = {"first": {"title": "Arabian Amber Nuit", "notes": ["Amber", "Rose", "Grapefruit", "Bergamot", "Pink Pepper"]}, "second": {"title": "Leather Oud", "notes": ["Leather", "Suede", "Raspberry", "Amber"]}}


def _real_combo(pair):
    return {
        "type": "HYBRID",
        "internalProducts": [
            {"title": pair["first"]["title"], "contribution": "Freshness", "notes": pair["first"]["notes"]},
            {"title": pair["second"]["title"], "contribution": "Sweetness", "notes": pair["second"]["notes"]},
        ],
        "recommendedRatio": [
            {"productTitle": pair["first"]["title"], "ratioPercent": 50},
            {"productTitle": pair["second"]["title"], "ratioPercent": 50},
        ],
        "customerFacingName": f"{pair['first']['title']} x {pair['second']['title']}",
    }


def _conversation_id(label: str) -> str:
    return f"pytest-shortcircuit-{label}-{time.time()}-{uuid.uuid4().hex[:8]}"


async def _cleanup(session, conversation_id: str):
    await session.execute(delete(CustomerProfileState).where(CustomerProfileState.conversationId == conversation_id))
    await session.commit()


@pytest.mark.asyncio
async def test_resolves_bare_1_against_most_recent_batch(db_session):
    conversation_id = _conversation_id("numeric")
    profile = {"city": "Los Angeles", "stateRegion": "California", "country": "United States", "likes": ["Fruity"], "dislikes": []}
    try:
        first_id = await save_recommendation(db_session, conversation_id=conversation_id, profile=profile, combination=_real_combo(_PAIR_A))
        await save_recommendation(db_session, conversation_id=conversation_id, profile=profile, combination=_real_combo(_PAIR_B))

        result = await resolve_legacy_preview_short_circuit(db_session, conversation_id, "1", "Test", "test@example.com", SHOP_DOMAIN)
        assert result is not None
        assert result["recommendationId"] == first_id
        # Phase 1 changed this contract (the preview URL now carries a build capability, `bt=`);
        # the assertion was hidden behind the missing-catalog failure until Phase 7.
        assert result["previewUrl"].startswith(build_preview_url(SHOP_DOMAIN, first_id)) and "&bt=" in result["previewUrl"]

        record = await db_session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == first_id))
        assert record.status == "confirmed"
    finally:
        await _cleanup(db_session, conversation_id)


@pytest.mark.asyncio
async def test_bare_preview_reopens_already_confirmed(db_session):
    conversation_id = _conversation_id("preview")
    profile = {"city": "Los Angeles", "stateRegion": "California", "country": "United States", "likes": ["Fruity"], "dislikes": []}
    try:
        rec_id = await save_recommendation(db_session, conversation_id=conversation_id, profile=profile, combination=_real_combo(_PAIR_A))
        first = await resolve_legacy_preview_short_circuit(db_session, conversation_id, "preview", "Test", "test@example.com", SHOP_DOMAIN)
        assert first["recommendationId"] == rec_id

        second = await resolve_legacy_preview_short_circuit(db_session, conversation_id, "preview", "Test", "test@example.com", SHOP_DOMAIN)
        assert second is not None
        assert second["recommendationId"] == rec_id
    finally:
        await _cleanup(db_session, conversation_id)


@pytest.mark.asyncio
async def test_returns_none_for_ordinary_conversational_text(db_session):
    conversation_id = _conversation_id("noop")
    result = await resolve_legacy_preview_short_circuit(db_session, conversation_id, "I'd like something fresh and citrusy", "Test", "test@example.com", SHOP_DOMAIN)
    assert result is None
