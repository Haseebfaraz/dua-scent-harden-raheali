"""Phase 7 -- conversation & recommendation intelligence. Semantic/state assertions on the
deterministic scaffolding (readiness gate, reasoning-bridge plumbing, refinement's dislike
persistence, high-signal detection), not on exact bot prose -- the model's own wording stays free
to vary, per the explicit instruction not to templatize it to satisfy tests.
"""

import uuid

from sqlalchemy import delete

from app.ai import conversation_flow
from app.ai.conversation_flow import call_ai
from app.ai.prompt import build_system_prompt, detect_high_signal_flags, determine_conversation_mode, should_offer_fragrance_pivot
from app.ai.tool_executor import execute_fragrance_tool
from app.db.models import CustomerProfileState
from app.services.customer_profile import empty_profile, get_customer_profile, save_customer_profile_fields

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
    # The early-phase template is a distinctly short GENERAL_CONVERSATION prompt -- not the full
    # fragrance-expert system prompt with all its vocabulary/tool guidance. A single bare greeting
    # is also below the 2-meaningful-turn bar, so no fragrance pivot is offered yet either.
    assert "conversationMode = GENERAL_CONVERSATION" in prompt
    assert "Do not manufacture several rounds of small talk" in prompt
    assert "Do not introduce fragrance yet" in prompt


async def test_bare_name_reply_after_greeting_still_gets_the_early_phase_prompt(db_session):
    conversation_id = _conversation_id("name-only")
    # The exact regression this guards: a customer's SECOND message being nothing but their name
    # must not, by message count alone, exit small talk into the full fragrance-consultant prompt.
    history = [
        {"role": "user", "content": "hey"},
        {"role": "assistant", "content": "Hey! How's your day going?"},
        {"role": "user", "content": "Haseeb"},
    ]
    prompt = await build_system_prompt(db_session, history, conversation_id, None, None)
    assert "conversationMode = GENERAL_CONVERSATION" in prompt


async def test_general_conversation_mode_deterministically_withholds_fragrance_tools(db_session, monkeypatch):
    # The structural guarantee behind conversationMode = GENERAL_CONVERSATION: even if the model
    # wanted to call analyze_customer_product_candidates or generate_new_product_combinations, it
    # cannot -- they are not in the request at all. Prompt wording alone was verified live to be
    # unreliable at temperature > 0; this asserts the actual tools payload, not the prose.
    from app.ai.tools import GENERAL_CONVERSATION_TOOLS

    captured_tools = []

    async def _fake_call_openai_once(messages, tools):
        captured_tools.append(tools)
        return {"choices": [{"finish_reason": "stop", "message": {"content": "Nice to meet you! What are you up to today?"}}]}

    monkeypatch.setattr(conversation_flow, "call_openai_once", _fake_call_openai_once)
    conversation_id = _conversation_id("mode-general")
    history = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hey! How's your day going?"},
        {"role": "user", "content": "Haseeb"},
    ]
    await call_ai(db_session, history, conversation_id, None, None, SHOP_DOMAIN)
    assert captured_tools[0] == GENERAL_CONVERSATION_TOOLS


async def test_fragrance_discovery_mode_gets_the_full_tool_set(db_session, monkeypatch):
    from app.ai.tools import FRAGRANCE_AGENT_TOOLS

    captured_tools = []

    async def _fake_call_openai_once(messages, tools, tool_choice=None):
        if tool_choice:
            return {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}  # batch-extraction pre-pass: nothing to extract
        captured_tools.append(tools)
        return {"choices": [{"finish_reason": "stop", "message": {"content": "Let's find you something fresh for the wedding."}}]}

    monkeypatch.setattr(conversation_flow, "call_openai_once", _fake_call_openai_once)
    conversation_id = _conversation_id("mode-fragrance")
    history = [{"role": "user", "content": "I need a fresh scent for my wedding"}]
    await call_ai(db_session, history, conversation_id, None, None, SHOP_DOMAIN)
    assert captured_tools[0] == FRAGRANCE_AGENT_TOOLS


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

async def test_analyze_candidates_rejects_style_and_occasion_alone(db_session):
    # Phase 8: discovery completeness -- style + occasion alone (the old Phase 7 bar) is no longer
    # enough; dislikes, performance, and location are still ungathered.
    conversation_id = _conversation_id("readiness-incomplete")
    ctx = {"conversationId": conversation_id, "customerName": None, "customerEmail": None, "shopDomain": SHOP_DOMAIN}
    try:
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "preferredStyle", "value": "fresh"}', ctx)
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "occasion", "value": "wedding"}', ctx)
        result = await execute_fragrance_tool(db_session, "analyze_customer_product_candidates", "{}", ctx)
        assert result["modelContent"].startswith("Error")
        assert "not enough signal" in result["modelContent"].lower()
    finally:
        await _cleanup(db_session, conversation_id)


async def test_analyze_candidates_succeeds_once_every_discovery_dimension_is_covered(db_session):
    conversation_id = _conversation_id("readiness-complete")
    ctx = {"conversationId": conversation_id, "customerName": None, "customerEmail": None, "shopDomain": SHOP_DOMAIN}
    try:
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "preferredStyle", "value": "fresh"}', ctx)
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "occasion", "value": "wedding"}', ctx)
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "dislikesAsked", "value": true}', ctx)
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "strengthPreference", "value": "moderate"}', ctx)
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "locationAsked", "value": true}', ctx)
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

    async def _fake_call_openai_once(messages, use_tools, tool_choice=None):
        if tool_choice:
            return {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}  # batch-extraction pre-pass: nothing to extract
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

    assert calls[0] and calls[1] is None  # first turn: tools enabled; follow-up: tools disabled
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


# ---------------------------------------------------------------------------
# Humanized soft fragrance pivot -- Python decides WHEN a pivot is eligible, the model still
# decides whether this turn is actually the right moment to take it.
# ---------------------------------------------------------------------------

def test_bare_hello_is_not_yet_pivot_eligible():
    profile = empty_profile()
    history = [{"role": "user", "content": "hello"}]
    assert should_offer_fragrance_pivot(history, profile) is False


def test_second_meaningful_turn_makes_the_pivot_eligible():
    profile = empty_profile()
    history = [
        {"role": "user", "content": "hey"},
        {"role": "assistant", "content": "Hey! How's it going?"},
        {"role": "user", "content": "pretty good, just relaxing"},
    ]
    assert should_offer_fragrance_pivot(history, profile) is True


def test_robot_hobby_conversation_becomes_pivot_eligible_after_two_turns():
    profile = empty_profile()
    # Deliberately avoids the word "project" -- detect_high_signal_flags' strength_or_longevity
    # group matches bare "project" (meant for fragrance projection), which would otherwise collide
    # with "robot project" and switch modes for an unrelated reason before the pivot check runs.
    history = [
        {"role": "user", "content": "hey"},
        {"role": "assistant", "content": "Hey! What are you up to?"},
        {"role": "user", "content": "building a little robot from scratch"},
    ]
    assert should_offer_fragrance_pivot(history, profile) is True
    # A robot hobby mention alone is not fragrance intent -- mode stays GENERAL_CONVERSATION,
    # it's the pivot that becomes eligible, not a forced switch to FRAGRANCE_DISCOVERY.
    assert determine_conversation_mode(history, profile) == "GENERAL_CONVERSATION"


def test_long_day_mood_conversation_becomes_pivot_eligible():
    profile = empty_profile()
    history = [
        {"role": "user", "content": "hey"},
        {"role": "assistant", "content": "Hey! How's your day going?"},
        {"role": "user", "content": "long day at work"},
    ]
    assert should_offer_fragrance_pivot(history, profile) is True
    assert determine_conversation_mode(history, profile) == "GENERAL_CONVERSATION"


async def test_decline_is_persisted_and_blocks_further_pivot_eligibility(db_session):
    conversation_id = _conversation_id("pivot-decline")
    try:
        await save_customer_profile_fields(db_session, conversation_id, {"fragrancePivotOffered": True})
        history = [
            {"role": "user", "content": "hey"},
            {"role": "assistant", "content": "Hey! How's it going?"},
            {"role": "user", "content": "pretty good"},
            {"role": "assistant", "content": "Nice. I could also put a fragrance together for you if you're up for it -- want me to try?"},
            {"role": "user", "content": "no thanks"},
        ]
        # build_system_prompt deterministically detects the decline and persists it -- not left
        # for the model to remember to call save_customer_profile_field itself.
        await build_system_prompt(db_session, history, conversation_id, None, None)
        profile = await get_customer_profile(db_session, conversation_id)
        assert profile["fragrancePivotDeclined"] is True
        assert should_offer_fragrance_pivot(history, profile) is False
    finally:
        await _cleanup(db_session, conversation_id)


def test_contextual_yeah_after_pivot_offer_switches_to_fragrance_discovery():
    profile = {**empty_profile(), "fragrancePivotOffered": True}
    history = [
        {"role": "user", "content": "hey"},
        {"role": "assistant", "content": "Hey! How's it going?"},
        {"role": "user", "content": "pretty good"},
        {"role": "assistant", "content": "I could turn that vibe into a fragrance direction too. Want me to try?"},
        {"role": "user", "content": "yeah go for it"},
    ]
    assert determine_conversation_mode(history, profile) == "FRAGRANCE_DISCOVERY"


def test_bare_yeah_without_a_prior_pivot_offer_is_not_fragrance_intent():
    # The same short reply must NOT count as fragrance intent in an unrelated context -- only when
    # it's actually answering a pivot invitation the assistant just made.
    profile = empty_profile()
    history = [
        {"role": "user", "content": "hey"},
        {"role": "assistant", "content": "Hey! Want to hear a fun fact?"},
        {"role": "user", "content": "yeah go for it"},
    ]
    assert determine_conversation_mode(history, profile) == "GENERAL_CONVERSATION"


async def test_direct_fragrance_request_skips_general_conversation_entirely(db_session):
    conversation_id = _conversation_id("direct-request")
    history = [{"role": "user", "content": "I need a fresh scent for my wedding"}]
    profile = await get_customer_profile(db_session, conversation_id)
    assert determine_conversation_mode(history, profile) == "FRAGRANCE_DISCOVERY"
    assert should_offer_fragrance_pivot(history, profile) is False  # no pivot needed, already there
