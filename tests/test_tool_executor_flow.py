import json
import time
import uuid

import pytest
from sqlalchemy import delete, select

from app.ai.refinement import derive_refinement_adjustments
from app.ai.preview_url import build_preview_url
from app.ai.tool_executor import _redact_titles_for_sse, execute_fragrance_tool
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
    # Also resolves the other Phase 8 discovery dimensions (occasion/dislikes/performance) this
    # file's tests don't care about, via their *Asked-equivalent -- these tests are about
    # generate/refine behavior itself, not discovery-completeness, which has its own dedicated
    # tests in test_customer_profile.py and test_conversation_intelligence.py.
    await save_customer_profile_fields(session, conversation_id, {
        "city": "Los Angeles", "country": "United States", "locationVerified": True, "locationSource": "order_history",
        "occasionAsked": True, "dislikesAsked": True, "strengthPreference": "moderate", "nameAsked": True,
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
        # Phase 1 (security): the preview URL carries a server-minted build capability (`bt`)
        # that authorizes exactly this recommendation -- see tests/security/test_build_capability.py.
        assert result["sseEvent"]["previewUrl"].startswith(build_preview_url(SHOP_DOMAIN, result["sseEvent"]["recommendationId"]) + "&bt=")
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
async def test_missing_customer_identity_is_never_reported_as_an_availability_problem(db_session):
    # The exact live bug this guards: buildable candidates existed (inventoryValidated=true,
    # buildable=true, real maxBuildableBottles) but confirm_recommendation's identity check --
    # checked before any candidate-specific logic -- rejected every one of them identically, and
    # the generic post-loop fallback used to collapse that into a misleading "availability snag"
    # message. Missing customer identity must surface as its own distinct, honest reason.
    conversation_id = _conversation_id("no-identity")
    ctx = {"conversationId": conversation_id, "customerName": None, "customerEmail": None, "shopDomain": SHOP_DOMAIN}
    try:
        await _verify_los_angeles_without_network(db_session, conversation_id)
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "likes", "value": ["Fruity"]}', ctx)

        result = await execute_fragrance_tool(db_session, "generate_new_product_combinations", "{}", ctx)

        # Asserting on the internal guidance text's exact wording (which necessarily names the
        # wrong framing in order to warn against it) is the wrong layer to test -- what matters is
        # that it correctly identifies THIS as an identity problem with real buildable stock,
        # never as a stock/inventory shortage.
        assert result["modelContent"].startswith("Error")
        assert "buildable combination exists" in result["modelContent"].lower()
        assert "sign" in result["modelContent"].lower() or "account" in result["modelContent"].lower()
        assert not result["sseEvent"]
    finally:
        await _cleanup(db_session, conversation_id)


@pytest.mark.asyncio
async def test_identity_rejection_is_logged_with_stage_reason_and_counts(db_session, caplog):
    import logging

    conversation_id = _conversation_id("identity-logs")
    ctx = {"conversationId": conversation_id, "customerName": None, "customerEmail": None, "shopDomain": SHOP_DOMAIN}
    try:
        await _verify_los_angeles_without_network(db_session, conversation_id)
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "likes", "value": ["Fruity"]}', ctx)

        with caplog.at_level(logging.INFO, logger="app.ai.tool_executor"):
            await execute_fragrance_tool(db_session, "generate_new_product_combinations", "{}", ctx)

        rejected_records = [r for r in caplog.records if "AUTOSELECT_CANDIDATE_REJECTED" in r.message]
        assert rejected_records, "expected at least one AUTOSELECT_CANDIDATE_REJECTED log line"
        rejected_payload = json.loads(rejected_records[0].message.split("AUTOSELECT_CANDIDATE_REJECTED ", 1)[1])
        # buildable is the field that actually matters here (Odoo is unreachable in this test
        # environment, so inventoryValidated legitimately comes back false per the established
        # "never claim inventory confirmed, but don't block on it" policy -- buildable=true is
        # what the live bug report showed too).
        assert rejected_payload["buildable"] is True
        assert rejected_payload["rejectionStage"] == "confirmation:identity_missing"

        failed_records = [r for r in caplog.records if "AUTOSELECT_FAILED" in r.message]
        assert failed_records, "expected a final AUTOSELECT_FAILED log line"
        failed_payload = json.loads(failed_records[0].message.split("AUTOSELECT_FAILED ", 1)[1])
        assert failed_payload["reason"] == "identity_missing"
        assert failed_payload["buildableCandidateCount"] >= 1
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
        assert result["sseEvent"]["previewUrl"].startswith(build_preview_url(SHOP_DOMAIN, result["sseEvent"]["recommendationId"]) + "&bt=")

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


def test_redact_titles_for_sse_strips_product_titles_but_keeps_other_fields():
    # sse_events reach the customer's browser network payload unfiltered -- product titles are
    # internal evidence and must never leave the backend that way, even though the same data (with
    # titles) is still what modelContent gives the LLM for its own internal reasoning.
    candidates = [{
        "productName": "Midnight Saffron Reserve", "normalizedProductName": "midnight saffron reserve",
        "relevanceScore": 12.5, "sameCityOrders": 3,
    }]
    redacted = _redact_titles_for_sse(candidates)
    assert redacted == [{"relevanceScore": 12.5, "sameCityOrders": 3}]
    assert "productName" not in redacted[0]
    assert "normalizedProductName" not in redacted[0]
    # Original list is untouched -- callers still pass the full candidates to modelContent.
    assert candidates[0]["productName"] == "Midnight Saffron Reserve"
