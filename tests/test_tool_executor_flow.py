import time
import uuid

import pytest
from sqlalchemy import delete, select

from app.ai.refinement import derive_refinement_adjustments
from app.ai.preview_url import build_preview_url
from app.ai.tool_executor import execute_fragrance_tool
from app.db.models import CustomerProfileState, FragranceRecommendation
from app.services import copy_generation
from app.services.customer_profile import get_customer_profile, save_customer_profile_fields
from app.services.recommendation_engine import has_hard_excluded_family

SHOP_DOMAIN = "test-shop.myshopify.com"


@pytest.fixture(autouse=True)
def _no_real_openai_calls(monkeypatch):
    async def _no_op(*args, **kwargs):
        return None

    monkeypatch.setattr(copy_generation, "call_copy_model", _no_op)


def _conversation_id(label: str) -> str:
    return f"pytest-autopreview-{label}-{time.time()}-{uuid.uuid4().hex[:8]}"


def _ctx(conversation_id: str) -> dict:
    return {"conversationId": conversation_id, "customerName": "Test Customer", "customerEmail": "test@example.com", "shopDomain": SHOP_DOMAIN}


async def _verify_los_angeles_without_network(session, conversation_id: str) -> None:
    await save_customer_profile_fields(session, conversation_id, {
        "city": "Los Angeles", "country": "United States", "locationVerified": True, "locationSource": "order_history",
    })


async def _cleanup(session, conversation_id: str) -> None:
    await session.execute(delete(CustomerProfileState).where(CustomerProfileState.conversationId == conversation_id))
    await session.commit()


@pytest.mark.asyncio
async def test_generate_emits_preview_ready_for_best_recommendation(db_session):
    conversation_id = _conversation_id("best")
    ctx = _ctx(conversation_id)
    try:
        await _verify_los_angeles_without_network(db_session, conversation_id)
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "likes", "value": ["Fruity"]}', ctx)

        result = await execute_fragrance_tool(db_session, "generate_new_product_combinations", "{}", ctx)

        assert not result["modelContent"].startswith("Error")
        assert result["sseEvent"]
        assert result["sseEvent"]["type"] == "preview_ready"
        assert result["sseEvent"]["recommendationId"]
        assert result["sseEvent"]["previewUrl"] == build_preview_url(SHOP_DOMAIN, result["sseEvent"]["recommendationId"])
        assert result["sseEvent"]["type"] != "combination_recommendations"

        record = await db_session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == result["sseEvent"]["recommendationId"]))
        assert record.status == "confirmed"

        # Phase 7: the model must be handed real, grounded facts about the winning combination so
        # it can write an actual reasoning bridge -- not just told to say nothing further.
        assert "whySuits" in result["modelContent"]
        assert "bestUse" in result["modelContent"]
        assert "reasoning bridge" in result["modelContent"].lower()
    finally:
        await _cleanup(db_session, conversation_id)


@pytest.mark.asyncio
async def test_generate_tells_model_not_to_list_or_ask(db_session):
    conversation_id = _conversation_id("instruction")
    ctx = _ctx(conversation_id)
    try:
        await _verify_los_angeles_without_network(db_session, conversation_id)
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "likes", "value": ["Fruity"]}', ctx)
        result = await execute_fragrance_tool(db_session, "generate_new_product_combinations", "{}", ctx)
        assert "do not list" in result["modelContent"].lower() or "do not ask" in result["modelContent"].lower()
    finally:
        await _cleanup(db_session, conversation_id)


@pytest.mark.asyncio
async def test_refine_emits_preview_ready_not_a_list(db_session):
    conversation_id = _conversation_id("refine")
    ctx = _ctx(conversation_id)
    try:
        await _verify_los_angeles_without_network(db_session, conversation_id)
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "likes", "value": ["Fruity"]}', ctx)
        await execute_fragrance_tool(db_session, "generate_new_product_combinations", "{}", ctx)

        result = await execute_fragrance_tool(db_session, "refine_combination_recommendations", '{"feedback": "make it fresher"}', ctx)

        assert not result["modelContent"].startswith("Error")
        assert result["sseEvent"]["type"] == "preview_ready"
        assert result["sseEvent"]["type"] != "recommendation_refined"
        assert result["sseEvent"]["previewUrl"] == build_preview_url(SHOP_DOMAIN, result["sseEvent"]["recommendationId"])

        record = await db_session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == result["sseEvent"]["recommendationId"]))
        assert record.status == "confirmed"
    finally:
        await _cleanup(db_session, conversation_id)


def test_derive_refinement_negation_direction():
    result = derive_refinement_adjustments("less sweet")
    assert "Sweet" in result["addDislikes"]
    assert "Sweet" not in result["addLikes"]


@pytest.mark.asyncio
async def test_refine_carries_unrecognized_keyword_family_into_profile(db_session):
    conversation_id = _conversation_id("refine-dislike")
    ctx = _ctx(conversation_id)
    try:
        await _verify_los_angeles_without_network(db_session, conversation_id)
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "likes", "value": ["Fruity"]}', ctx)
        await execute_fragrance_tool(db_session, "generate_new_product_combinations", "{}", ctx)

        result = await execute_fragrance_tool(db_session, "refine_combination_recommendations", '{"feedback": "no musk please"}', ctx)
        assert not result["modelContent"].startswith("Error")
        assert result["sseEvent"]["type"] == "preview_ready"

        record = await db_session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == result["sseEvent"]["recommendationId"]))
        assert "Musk" in record.customerProfileJson["dislikes"]
    finally:
        await _cleanup(db_session, conversation_id)


@pytest.mark.asyncio
async def test_refine_hard_excludes_named_family(db_session):
    conversation_id = _conversation_id("refine-hardexclude")
    ctx = _ctx(conversation_id)
    try:
        await _verify_los_angeles_without_network(db_session, conversation_id)
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "likes", "value": ["Fruity", "Apple", "Strawberry", "Peach"]}', ctx)
        await execute_fragrance_tool(db_session, "generate_new_product_combinations", "{}", ctx)

        result = await execute_fragrance_tool(db_session, "refine_combination_recommendations", '{"feedback": "dont want sandalwood"}', ctx)
        assert not result["modelContent"].startswith("Error")
        assert result["sseEvent"]["type"] == "preview_ready"

        record = await db_session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == result["sseEvent"]["recommendationId"]))
        all_notes = [n for p in record.productsJson for n in (p.get("notes") or [])]
        assert not any("sandalwood" in n.lower() for n in all_notes)
        assert has_hard_excluded_family(all_notes, ["woody"]) is False
    finally:
        await _cleanup(db_session, conversation_id)


@pytest.mark.asyncio
async def test_refine_recognizes_note_with_no_family_entry(db_session):
    conversation_id = _conversation_id("refine-orphan-note")
    ctx = _ctx(conversation_id)
    try:
        await _verify_los_angeles_without_network(db_session, conversation_id)
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "likes", "value": ["Fruity", "Pear", "Guava", "Pineapple"]}', ctx)

        from app.services.recommendation_confirmation import save_recommendation

        seeded_combination = {
            "type": "HYBRID",
            "canonicalKey": f"pytest-orphan-{time.time()}",
            "internalProducts": [
                {"title": "Juicy Pear", "contribution": "Freshness", "notes": ["Pear", "Guava", "Melon", "Pineapple", "Jackfruit", "Vanilla", "Gin", "Mojito", "and Musk"]},
                {"title": "Water of Arabia", "contribution": "Sweetness", "notes": ["Mandarin", "Bergamot", "Blackcurrant", "Green Tea", "Sandalwood"]},
            ],
            "recommendedRatio": [{"productTitle": "Juicy Pear", "ratioPercent": 50}, {"productTitle": "Water of Arabia", "ratioPercent": 50}],
            "customerFacingName": "Test Blend",
        }
        seeded_id = await save_recommendation(db_session, conversation_id=conversation_id, profile={"likes": ["Fruity", "Pear", "Guava", "Pineapple"], "dislikes": []}, combination=seeded_combination)
        await save_customer_profile_fields(db_session, conversation_id, {"selectedRecommendationId": seeded_id})

        result = await execute_fragrance_tool(db_session, "refine_combination_recommendations", '{"feedback": "dont want jackfruit"}', ctx)
        assert not result["modelContent"].startswith("Error")
        assert result["sseEvent"]["type"] == "preview_ready"

        record = await db_session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == result["sseEvent"]["recommendationId"]))
        all_notes = [n for p in record.productsJson for n in (p.get("notes") or [])]
        assert not any("jackfruit" in n.lower() for n in all_notes)

        profile = await get_customer_profile(db_session, conversation_id)
        assert "jackfruit" in profile["dislikes"]
    finally:
        await _cleanup(db_session, conversation_id)


@pytest.mark.asyncio
async def test_refine_persists_refinement_into_stored_profile(db_session):
    conversation_id = _conversation_id("refine-persist")
    ctx = _ctx(conversation_id)
    try:
        await _verify_los_angeles_without_network(db_session, conversation_id)
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "likes", "value": ["Fruity"]}', ctx)
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "dislikes", "value": ["vanilla"]}', ctx)
        await execute_fragrance_tool(db_session, "generate_new_product_combinations", "{}", ctx)

        result = await execute_fragrance_tool(db_session, "refine_combination_recommendations", '{"feedback": "dont want sandalwood"}', ctx)
        assert not result["modelContent"].startswith("Error")

        profile = await get_customer_profile(db_session, conversation_id)
        assert "vanilla" in profile["dislikes"]
        assert "sandalwood" in profile["dislikes"]
    finally:
        await _cleanup(db_session, conversation_id)


@pytest.mark.asyncio
async def test_refine_noop_leaves_profile_unchanged(db_session):
    conversation_id = _conversation_id("refine-persist-noop")
    ctx = _ctx(conversation_id)
    try:
        await _verify_los_angeles_without_network(db_session, conversation_id)
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "likes", "value": ["Fruity"]}', ctx)
        await execute_fragrance_tool(db_session, "generate_new_product_combinations", "{}", ctx)

        await execute_fragrance_tool(db_session, "refine_combination_recommendations", '{"feedback": "give me a Hybrid only"}', ctx)

        profile = await get_customer_profile(db_session, conversation_id)
        assert profile["likes"] == ["Fruity"]
        assert profile["dislikes"] == []
    finally:
        await _cleanup(db_session, conversation_id)


@pytest.mark.asyncio
async def test_select_recommendation_rehydrates_from_db_when_scratch_empty(db_session):
    conversation_id = _conversation_id("legacy-select")
    from app.services.recommendation_confirmation import save_recommendation

    profile = {"city": "Los Angeles", "stateRegion": "California", "country": "United States", "likes": ["Fruity"], "dislikes": []}

    def combo(title):
        return {
            "type": "HYBRID",
            "internalProducts": [
                {"title": title, "contribution": "Freshness", "notes": ["Bergamot", "Musk"]},
                {"title": f"{title} B", "contribution": "Sweetness", "notes": ["Vanilla"]},
            ],
            "recommendedRatio": [{"productTitle": title, "ratioPercent": 50}, {"productTitle": f"{title} B", "ratioPercent": 50}],
            "customerFacingName": f"{title} Blend",
        }

    try:
        first_id = await save_recommendation(db_session, conversation_id=conversation_id, profile=profile, combination=combo("Alpha"))
        second_id = await save_recommendation(db_session, conversation_id=conversation_id, profile=profile, combination=combo("Beta"))

        result = await execute_fragrance_tool(db_session, "select_recommendation", '{"selectionText": "2"}', _ctx(conversation_id))
        assert not result["modelContent"].startswith("Error")
        assert result["sseEvent"]["recommendationId"] == second_id
    finally:
        await _cleanup(db_session, conversation_id)
