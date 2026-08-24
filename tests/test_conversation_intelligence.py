"""Phase 7 -- conversation & recommendation intelligence. Semantic/state assertions on the
deterministic scaffolding (readiness gate, reasoning-bridge plumbing, refinement's dislike
persistence, high-signal detection), not on exact bot prose -- the model's own wording stays free
to vary, per the explicit instruction not to templatize it to satisfy tests.
"""

import uuid

from sqlalchemy import delete

from app.ai import conversation_flow
from app.ai.conversation_flow import call_ai
from app.ai.prompt import build_system_prompt, detect_high_signal_flags
from app.ai.tool_executor import execute_fragrance_tool
from app.db.models import CustomerProfileState
from app.services.customer_profile import get_customer_profile, save_customer_profile_fields

SHOP_DOMAIN = "test-shop.myshopify.com"


async def _cleanup(session, conversation_id: str) -> None:
    await session.execute(delete(CustomerProfileState).where(CustomerProfileState.conversationId == conversation_id))
    await session.commit()


def _conversation_id(label: str) -> str:
    return f"pytest-convo-{label}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Section 4/16: high-signal detection
# ---------------------------------------------------------------------------

def test_high_signal_flags_detect_occasion():
    assert "occasion" in detect_high_signal_flags("I have a work party Friday")


def test_high_signal_flags_detect_dislike_or_sensitivity():
    assert "dislike_or_sensitivity" in detect_high_signal_flags("anything strong gives me a headache")


def test_high_signal_flags_empty_for_plain_greeting():
    assert detect_high_signal_flags("hey") == []


# ---------------------------------------------------------------------------
# Section 1/16: early-phase small talk vs. following expressed intent
# ---------------------------------------------------------------------------

async def test_bare_greeting_gets_the_short_early_phase_prompt(db_session):
    conversation_id = _conversation_id("greeting")
    history = [{"role": "user", "content": "hey"}]
    prompt = await build_system_prompt(db_session, history, conversation_id, None, "Alex")
    # The early-phase template is a distinctly short, single-question prompt -- not the full
    # fragrance-expert system prompt with all its vocabulary/tool guidance.
    assert "Keep this reply short and natural" in prompt
    assert "Do NOT manufacture several rounds of small talk" in prompt


async def test_direct_fragrance_intent_skips_the_early_phase_small_talk_prompt(db_session):
    conversation_id = _conversation_id("direct-intent")
    history = [{"role": "user", "content": "I need something fresh for my wedding"}]
    prompt = await build_system_prompt(db_session, history, conversation_id, None, "Alex")
    # A message with real fragrance/occasion intent on the very first turn must NOT get the
    # small-talk-first template -- it goes straight to the full conversational prompt instead.
    assert "Keep this reply short and natural" not in prompt


# ---------------------------------------------------------------------------
# Section 3: full context -- known profile facts are visible to every prompt build
# ---------------------------------------------------------------------------

async def test_known_profile_facts_are_always_injected_into_the_prompt(db_session):
    conversation_id = _conversation_id("context")
    await save_customer_profile_fields(db_session, conversation_id, {"occasion": "work party", "preferredStyle": "fresh"})
    history = [{"role": "user", "content": "hey"}, {"role": "assistant", "content": "..."}, {"role": "user", "content": "anyway what do you think"}]
    prompt = await build_system_prompt(db_session, history, conversation_id, None, None)
    assert "work party" in prompt
    assert "fresh" in prompt


# ---------------------------------------------------------------------------
# Section 10: confidence-based recommendation readiness (also see test_customer_profile.py)
# ---------------------------------------------------------------------------

async def test_analyze_candidates_succeeds_with_style_and_occasion_alone_no_location(db_session):
    conversation_id = _conversation_id("readiness")
    ctx = {"conversationId": conversation_id, "customerName": None, "customerEmail": None, "shopDomain": SHOP_DOMAIN}
    try:
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "preferredStyle", "value": "fresh"}', ctx)
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "occasion", "value": "wedding"}', ctx)
        result = await execute_fragrance_tool(db_session, "analyze_customer_product_candidates", "{}", ctx)
        assert not result["modelContent"].startswith("Error")
        assert "not enough signal" not in result["modelContent"].lower()
    finally:
        await _cleanup(db_session, conversation_id)


# ---------------------------------------------------------------------------
# Section 13: refinement preserves existing hard dislikes
# ---------------------------------------------------------------------------

async def test_refinement_never_drops_a_previously_saved_dislike(db_session):
    conversation_id = _conversation_id("refine-dislike")
    ctx = {"conversationId": conversation_id, "customerName": None, "customerEmail": None, "shopDomain": SHOP_DOMAIN}
    try:
        await save_customer_profile_fields(db_session, conversation_id, {
            "likes": ["Fruity"], "dislikes": ["Oud"], "city": "Los Angeles", "country": "United States", "locationVerified": True,
        })
        # "make it sweeter" says nothing about oud at all -- the earlier hard dislike must survive.
        await execute_fragrance_tool(db_session, "refine_combination_recommendations", '{"feedback": "make it sweeter"}', ctx)
        profile = await get_customer_profile(db_session, conversation_id)
        assert "Oud" in profile["dislikes"]
    finally:
        await _cleanup(db_session, conversation_id)


# ---------------------------------------------------------------------------
# Section 12: the reasoning bridge is the model's own text, not a canned line
# ---------------------------------------------------------------------------

async def test_call_ai_gives_the_model_a_second_turn_to_write_the_reasoning_bridge(db_session, monkeypatch):
    calls = []

    async def _fake_call_openai_once(messages, use_tools):
        calls.append(use_tools)
        if len(calls) == 1:
            return {"choices": [{"finish_reason": "tool_calls", "message": {
                "content": None,
                "tool_calls": [{"id": "call_1", "function": {"name": "generate_new_product_combinations", "arguments": "{}"}}],
            }}]}
        return {"choices": [{"finish_reason": "stop", "message": {
            "content": "You wanted something fresh for the wedding with no oud, so I kept the opening bright and the base clean.",
        }}]}

    async def _fake_execute_fragrance_tool(session, tool_name, args, context):
        return {
            "modelContent": "grounded facts: whySuits=..., bestUse=...",
            "sseEvent": {"type": "preview_ready", "recommendationId": "rec_pytest", "previewId": "rec_pytest", "previewUrl": "https://example.test/preview"},
        }

    monkeypatch.setattr(conversation_flow, "call_openai_once", _fake_call_openai_once)
    monkeypatch.setattr(conversation_flow, "execute_fragrance_tool", _fake_execute_fragrance_tool)

    conversation_id = _conversation_id("bridge")
    history = [{"role": "user", "content": "I need something fresh for my wedding, no oud."}]
    result = await call_ai(db_session, history, conversation_id, None, None, SHOP_DOMAIN)

    assert calls == [True, False]  # first turn: tools enabled; follow-up: tools disabled
    assert result["replyText"] == "You wanted something fresh for the wedding with no oud, so I kept the opening bright and the base clean."
    assert result["sseEvents"][0]["type"] == "preview_ready"


async def test_call_ai_falls_back_to_a_generic_line_if_the_bridge_completion_fails(db_session, monkeypatch):
    calls = []

    async def _fake_call_openai_once(messages, use_tools):
        calls.append(use_tools)
        if len(calls) == 1:
            return {"choices": [{"finish_reason": "tool_calls", "message": {
                "content": None,
                "tool_calls": [{"id": "call_1", "function": {"name": "generate_new_product_combinations", "arguments": "{}"}}],
            }}]}
        return None  # simulate the follow-up completion itself failing

    async def _fake_execute_fragrance_tool(session, tool_name, args, context):
        return {"modelContent": "grounded facts", "sseEvent": {"type": "preview_ready", "recommendationId": "rec_pytest", "previewId": "rec_pytest", "previewUrl": "https://example.test/preview"}}

    monkeypatch.setattr(conversation_flow, "call_openai_once", _fake_call_openai_once)
    monkeypatch.setattr(conversation_flow, "execute_fragrance_tool", _fake_execute_fragrance_tool)

    conversation_id = _conversation_id("bridge-fallback")
    history = [{"role": "user", "content": "surprise me"}]
    result = await call_ai(db_session, history, conversation_id, None, None, SHOP_DOMAIN)

    assert result["replyText"] == "I've got the blend ready — take a look."
