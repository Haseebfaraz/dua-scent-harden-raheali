"""Phase 7 -- conversation & recommendation intelligence. Semantic/state assertions on the
deterministic scaffolding (readiness gate, reasoning-bridge plumbing, refinement's dislike
persistence, high-signal detection), not on exact bot prose -- the model's own wording stays free
to vary, per the explicit instruction not to templatize it to satisfy tests.
"""

import json
import uuid

import pytest
from sqlalchemy import delete

from app.ai import conversation_flow
from app.ai.conversation_flow import call_ai
from app.ai.safe_views import customer_context_messages
from app.ai.prompt import (
    build_system_prompt,
    detect_high_signal_flags,
    determine_conversation_mode,
    is_custom_build_invitation_due,
    is_direct_custom_build_request,
    is_fragrance_pivot_due,
    is_role_question,
)
from app.ai.tool_executor import execute_fragrance_tool
from app.db.models import CustomerProfileState, FragranceRecommendation
from app.db.time import utcnow
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
    # is also well below the meaningful-turn bar, so the pivot is not due yet either.
    assert "conversationMode = GENERAL_CONVERSATION" in prompt
    assert "Do not manufacture several rounds of small talk" in prompt
    assert "FRAGRANCE PIVOT STATUS: NOT_DUE" in prompt


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

async def test_known_profile_facts_are_always_available_as_customer_context_data(db_session):
    # Phase 3 (N5): profile facts reach the model as a DATA message (a tool result), never as
    # prose inside the trusted system prompt.
    conversation_id = _conversation_id("context")
    await save_customer_profile_fields(db_session, conversation_id, {"occasion": "work party", "preferredStyle": "fresh"})
    history = [{"role": "user", "content": "hey"}, {"role": "assistant", "content": "..."}, {"role": "user", "content": "anyway what do you think"}]
    prompt = await build_system_prompt(db_session, history, conversation_id, None, None)
    assert "work party" not in prompt
    context = customer_context_messages(await get_customer_profile(db_session, conversation_id))
    assert context[1]["role"] == "tool" and "work party" in context[1]["content"] and "fresh" in context[1]["content"]


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
        await execute_fragrance_tool(db_session, "save_customer_profile_field", '{"field": "nameAsked", "value": true}', ctx)
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

def _ready_outcome():
    from app.ai.safe_views import CustomerSafeRecommendation
    from app.services.recommendation_pipeline import PipelineOutcome

    return PipelineOutcome(status="READY", recommendation_id="rec_pytest", preview_url="https://example.test/preview", safe_recommendation=CustomerSafeRecommendation(name="Test Blend", whyItMatches="fresh", bestFor="the wedding"))


async def test_call_ai_gives_the_model_a_bridge_turn_once_the_server_has_built_the_fragrance(db_session, monkeypatch):
    # Phase 3: the model never calls a generation tool. The server runs the private pipeline
    # when the profile is ready and then gives the model ONE completion, tools disabled, to write
    # the grounded bridge from the customer-safe summary.
    calls = []

    async def _fake_call_openai_once(messages, use_tools, tool_choice=None):
        if tool_choice:
            return {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}  # batch-extraction pre-pass: nothing to extract
        calls.append(use_tools)
        return {"choices": [{"finish_reason": "stop", "message": {
            "content": "You wanted something fresh for the wedding with no oud, so I kept the opening bright and the base clean.",
        }}]}

    async def _fake_pipeline(session, conversation_id, context):
        return _ready_outcome()

    monkeypatch.setattr(conversation_flow, "call_openai_once", _fake_call_openai_once)
    monkeypatch.setattr(conversation_flow, "should_generate", lambda cid, profile, mode: True)
    monkeypatch.setattr(conversation_flow, "run_private_recommendation", _fake_pipeline)

    conversation_id = _conversation_id("bridge")
    history = [{"role": "user", "content": "I need something fresh for my wedding, no oud."}]
    result = await call_ai(db_session, history, conversation_id, None, None, SHOP_DOMAIN)

    assert calls == [None]  # exactly one bridge completion, tools disabled
    assert result["replyText"] == "You wanted something fresh for the wedding with no oud, so I kept the opening bright and the base clean."
    assert result["sseEvents"][0]["type"] == "preview_ready" and result["sseEvents"][0]["recommendationId"] == "rec_pytest"


async def test_call_ai_falls_back_to_a_generic_line_if_the_bridge_completion_fails(db_session, monkeypatch):
    async def _fake_call_openai_once(messages, use_tools, tool_choice=None):
        if tool_choice:
            return {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}
        return None  # simulate the bridge completion itself failing

    async def _fake_pipeline(session, conversation_id, context):
        return _ready_outcome()

    monkeypatch.setattr(conversation_flow, "call_openai_once", _fake_call_openai_once)
    monkeypatch.setattr(conversation_flow, "should_generate", lambda cid, profile, mode: True)
    monkeypatch.setattr(conversation_flow, "run_private_recommendation", _fake_pipeline)

    conversation_id = _conversation_id("bridge-fallback")
    history = [{"role": "user", "content": "surprise me"}]
    result = await call_ai(db_session, history, conversation_id, None, None, SHOP_DOMAIN)

    assert result["replyText"] == "I've got the blend ready — take a look."


# ---------------------------------------------------------------------------
# Fragrance pivot DUE -- Python decides WHETHER and WHEN a pivot is mandatory (not merely
# eligible: a merely-eligible pivot let the model keep declining the opportunity forever, verified
# live over an 8-turn small-talk conversation that never once mentioned fragrance). The model still
# decides exact wording and whether THIS reply is a genuine emergency to handle first.
# ---------------------------------------------------------------------------

def test_bare_hello_is_not_yet_pivot_due():
    profile = empty_profile()
    history = [{"role": "user", "content": "hello"}]
    assert is_fragrance_pivot_due(history, profile) is False


def test_filler_only_turns_never_accumulate_toward_pivot_due():
    # hello / yes / hmm / okay-style replies carry no real context and must not count, even many
    # of them in a row.
    profile = empty_profile()
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hey! How's it going?"},
        {"role": "user", "content": "great yours?"},
        {"role": "assistant", "content": "Doing well, thanks!"},
        {"role": "user", "content": "yes it is"},
        {"role": "assistant", "content": "Glad to hear it."},
        {"role": "user", "content": "hmm"},
    ]
    assert is_fragrance_pivot_due(history, profile) is False


def test_four_meaningful_work_related_turns_makes_the_pivot_due():
    profile = empty_profile()
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hey! How's it going?"},
        {"role": "user", "content": "just back from vacations"},
        {"role": "assistant", "content": "Nice, hope it was a good reset."},
        {"role": "user", "content": "settling back"},
        {"role": "assistant", "content": "That shift can be a little rough."},
        {"role": "user", "content": "aligning by work"},
        {"role": "assistant", "content": "Work mode, then."},
        {"role": "user", "content": "trying to catch up on emails today"},
    ]
    assert is_fragrance_pivot_due(history, profile) is True
    # Four real, meaningful turns is not itself fragrance intent -- mode stays GENERAL_CONVERSATION,
    # it's the pivot that becomes due, not a forced switch to FRAGRANCE_DISCOVERY.
    assert determine_conversation_mode(history, profile) == "GENERAL_CONVERSATION"


def test_robot_hobby_conversation_becomes_pivot_due_after_rapport():
    profile = empty_profile()
    # Deliberately avoids the word "project" -- detect_high_signal_flags' strength_or_longevity
    # group matches bare "project" (meant for fragrance projection), which would otherwise collide
    # with "robot project" and switch modes for an unrelated reason before the pivot check runs.
    history = [
        {"role": "user", "content": "hey"},
        {"role": "assistant", "content": "Hey! What are you up to?"},
        {"role": "user", "content": "building a little robot from scratch"},
        {"role": "assistant", "content": "That's a fun hobby."},
        {"role": "user", "content": "yeah it's been keeping me busy on weekends"},
        {"role": "assistant", "content": "Sounds rewarding."},
        {"role": "user", "content": "definitely, learning a lot about motors and sensors"},
        {"role": "assistant", "content": "That's a real skill to build."},
        {"role": "user", "content": "trying to get the wiring right this week"},
    ]
    assert is_fragrance_pivot_due(history, profile) is True
    assert determine_conversation_mode(history, profile) == "GENERAL_CONVERSATION"


def test_role_question_forces_pivot_due_regardless_of_turn_count():
    profile = empty_profile()
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Hey! How's it going?"},
        {"role": "user", "content": "what is your task?"},
    ]
    assert is_role_question("what is your task?") is True
    assert is_fragrance_pivot_due(history, profile) is True


def test_role_question_does_not_force_pivot_after_decline():
    # A direct role question is a strong opening, but an explicit decline is still respected --
    # the assistant can explain what it does without re-pushing a declined fragrance offer.
    profile = {**empty_profile(), "fragrancePivotDeclined": True}
    history = [{"role": "user", "content": "what is your task?"}]
    assert is_fragrance_pivot_due(history, profile) is False


async def test_decline_is_persisted_and_blocks_further_pivot_due(db_session):
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
        assert is_fragrance_pivot_due(history, profile) is False
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
    assert is_fragrance_pivot_due(history, profile) is False  # no pivot needed, already there


async def test_due_pivot_prompt_hands_the_urgent_customer_judgment_to_the_model(db_session):
    # Python has no reliable "customer is upset"/"urgent unrelated issue" detector -- when due, the
    # injected prompt must still explicitly hand that judgment call to the model rather than
    # silently assuming every due turn is a safe moment to pivot.
    conversation_id = _conversation_id("due-escape-valve")
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "just back from vacations"},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "settling back"},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "aligning by work"},
    ]
    profile = await get_customer_profile(db_session, conversation_id)
    assert is_fragrance_pivot_due(history, profile) is True
    prompt = await build_system_prompt(db_session, history, conversation_id, None, None)
    assert "FRAGRANCE PIVOT STATUS: DUE" in prompt
    assert "you must introduce fragrance" in prompt.lower()
    assert "urgent" in prompt.lower() and "distressed" in prompt.lower()


# ---------------------------------------------------------------------------
# LIVE regression: the exact transcript that exposed the bug -- an 8-turn small-talk conversation
# that used to finish with the assistant still behaving like a generic chatbot. Real, paid calls
# through the actual call_ai orchestration -- run explicitly with `pytest -m live_ai`.
# ---------------------------------------------------------------------------

_LIVE_PIVOT_TRANSCRIPT = [
    "hello",
    "great yours?",
    "just back from vacations",
    "settling back",
    "aligning by work",
    "yes it is",
    "hmm",
    "what is your task?",
]


@pytest.mark.live_ai
async def test_live_transcript_introduces_fragrance_pivot_by_role_question(db_session):
    conversation_id = _conversation_id("live-pivot-transcript")
    history: list[dict] = []
    pivot_due_at_turn = None
    pivot_used_at_turn = None
    mode_before_pivot = None
    assistant_replies = []
    try:
        for turn_index, customer_message in enumerate(_LIVE_PIVOT_TRANSCRIPT, start=1):
            profile_before = await get_customer_profile(db_session, conversation_id)
            history.append({"role": "user", "content": customer_message})

            if pivot_due_at_turn is None and is_fragrance_pivot_due(history, profile_before):
                pivot_due_at_turn = turn_index
                mode_before_pivot = determine_conversation_mode(history, profile_before)

            result = await call_ai(db_session, history, conversation_id, None, None, SHOP_DOMAIN)
            reply_text = result["replyText"]
            assistant_replies.append(reply_text)
            history = result.get("updatedMessages") or (history + [{"role": "assistant", "content": reply_text}])

            profile_after = await get_customer_profile(db_session, conversation_id)
            if pivot_used_at_turn is None and profile_after.get("fragrancePivotOffered"):
                pivot_used_at_turn = turn_index

        mode_after_transcript = determine_conversation_mode(history, await get_customer_profile(db_session, conversation_id))

        print(f"\nPIVOT_DUE_AT_TURN={pivot_due_at_turn}")
        print(f"PIVOT_USED_AT_TURN={pivot_used_at_turn}")
        print(f"MODE_BEFORE_PIVOT={mode_before_pivot}")
        print(f"MODE_AFTER_TRANSCRIPT={mode_after_transcript}")
        for i, (msg, reply) in enumerate(zip(_LIVE_PIVOT_TRANSCRIPT, assistant_replies), start=1):
            print(f"  [{i}] Customer: {msg}")
            print(f"      Assistant: {reply}")

        assert pivot_used_at_turn is not None, "the pivot was never delivered across the whole transcript"
        assert pivot_used_at_turn <= 8, "the pivot must be introduced by turn 8 (the role question)"

        pivot_reply = assistant_replies[pivot_used_at_turn - 1].lower()
        assert "help with that later" not in pivot_reply
        assert "if you want fragrance help later" not in pivot_reply
        assert "fragrance help later" not in pivot_reply
        assert "dua" not in pivot_reply
    finally:
        await db_session.execute(delete(CustomerProfileState).where(CustomerProfileState.conversationId == conversation_id))
        await db_session.commit()


# ---------------------------------------------------------------------------
# Custom build invitation -- fragrance INTEREST (a bare style word like "fruity") is not the same
# as build ACCEPTANCE. Interest routes through a second, explicit invitation instead of jumping
# straight into a full discovery questionnaire.
# ---------------------------------------------------------------------------

def test_bare_style_word_alone_does_not_start_fragrance_discovery():
    profile = empty_profile()
    assert determine_conversation_mode([{"role": "user", "content": "fruity"}], profile) == "GENERAL_CONVERSATION"


def test_bare_style_word_makes_the_custom_build_invitation_due():
    profile = empty_profile()
    history = [{"role": "user", "content": "fruity"}]
    assert is_custom_build_invitation_due(history, profile) is True


def test_custom_build_invitation_not_due_without_any_fragrance_interest():
    profile = empty_profile()
    history = [{"role": "user", "content": "hey"}, {"role": "assistant", "content": "..."}, {"role": "user", "content": "just relaxing"}]
    assert is_custom_build_invitation_due(history, profile) is False


def test_custom_build_invitation_not_due_twice():
    profile = {**empty_profile(), "customBuildInvited": True}
    history = [{"role": "user", "content": "fruity"}]
    assert is_custom_build_invitation_due(history, profile) is False


def test_custom_build_invitation_not_due_after_decline():
    profile = {**empty_profile(), "customBuildDeclined": True}
    history = [{"role": "user", "content": "fruity"}]
    assert is_custom_build_invitation_due(history, profile) is False


@pytest.mark.parametrize("message", [
    "make me a fragrance",
    "build me a custom scent",
    "I want to create my own perfume",
    "can you make something for me",
    "create a fragrance for dinner",
])
def test_direct_custom_build_requests_are_detected(message):
    assert is_direct_custom_build_request(message) is True


@pytest.mark.parametrize("message", [
    "fruity", "fresh", "I like sweet scents", "oud is nice", "I usually wear clean scents",
    "vanilla", "something strong",
])
def test_fragrance_interest_without_build_intent_is_not_a_direct_request(message):
    assert is_direct_custom_build_request(message) is False


@pytest.mark.parametrize("message", [
    "make me a fragrance",
    "build me a custom scent",
    "I want to create my own perfume",
    "can you make something for me",
    "create a fragrance for dinner",
])
def test_direct_custom_build_requests_skip_the_invitation_entirely(message):
    profile = empty_profile()
    history = [{"role": "user", "content": message}]
    assert determine_conversation_mode(history, profile) == "FRAGRANCE_DISCOVERY"
    assert is_custom_build_invitation_due(history, profile) is False


def test_contextual_acceptance_of_the_custom_build_invitation_switches_mode():
    profile = {**empty_profile(), "customBuildInvited": True}
    history = [
        {"role": "user", "content": "fruity"},
        {"role": "assistant", "content": "I can build something around that. Want me to make one with you?"},
        {"role": "user", "content": "yeah sure"},
    ]
    assert determine_conversation_mode(history, profile) == "FRAGRANCE_DISCOVERY"


def test_bare_yes_without_a_pending_custom_build_invitation_is_not_acceptance():
    profile = empty_profile()
    history = [{"role": "user", "content": "yeah sure"}]
    assert determine_conversation_mode(history, profile) == "GENERAL_CONVERSATION"


async def test_custom_build_decline_is_persisted_and_blocks_further_invitation(db_session):
    conversation_id = _conversation_id("custom-build-decline")
    try:
        await save_customer_profile_fields(db_session, conversation_id, {"customBuildInvited": True, "likes": ["Fruity"]})
        history = [
            {"role": "user", "content": "fruity"},
            {"role": "assistant", "content": "I can build something around that. Want me to make one with you?"},
            {"role": "user", "content": "not now"},
        ]
        # build_system_prompt deterministically detects the decline and persists it -- not left
        # for the model to remember to call save_customer_profile_field itself.
        await build_system_prompt(db_session, history, conversation_id, None, None)
        profile = await get_customer_profile(db_session, conversation_id)
        assert profile["customBuildDeclined"] is True
        assert is_custom_build_invitation_due(history, profile) is False
        assert determine_conversation_mode(history, profile) == "GENERAL_CONVERSATION"
    finally:
        await _cleanup(db_session, conversation_id)


async def test_custom_build_acceptance_is_persisted_and_stays_sticky(db_session):
    # Once accepted, mode must stay FRAGRANCE_DISCOVERY even on a later turn where the customer's
    # latest message no longer looks like an acceptance itself (e.g. they've moved on to a normal
    # follow-up answer) -- a transient "was the last message yes" check alone would lose this.
    conversation_id = _conversation_id("custom-build-accept-sticky")
    try:
        await save_customer_profile_fields(db_session, conversation_id, {"customBuildInvited": True, "likes": ["Fruity"]})
        history = [
            {"role": "user", "content": "fruity"},
            {"role": "assistant", "content": "I can build something around that. Want me to make one with you?"},
            {"role": "user", "content": "yeah sure"},
        ]
        await build_system_prompt(db_session, history, conversation_id, None, None)
        profile = await get_customer_profile(db_session, conversation_id)
        assert profile["customBuildAccepted"] is True

        later_history = [*history, {"role": "assistant", "content": "Great. Is this for everyday wear or somewhere specific?"}, {"role": "user", "content": "everyday"}]
        assert determine_conversation_mode(later_history, profile) == "FRAGRANCE_DISCOVERY"
    finally:
        await _cleanup(db_session, conversation_id)


async def test_custom_build_invitation_status_block_appears_when_due(db_session):
    # Prompt-contract check (not just the deterministic gate): the model must actually be told
    # about the invitation, in mandatory language, when it's due.
    conversation_id = _conversation_id("custom-build-status-block")
    history = [{"role": "user", "content": "fruity"}]
    prompt = await build_system_prompt(db_session, history, conversation_id, None, None)
    assert "CUSTOM BUILD INVITATION DUE" in prompt
    assert "invite them" in prompt.lower()
    assert "do not start a fragrance preference questionnaire yet" in prompt.lower()


# ---------------------------------------------------------------------------
# Strict customer-facing brand + product-title privacy
# ---------------------------------------------------------------------------

async def test_reasoning_bridge_never_leaks_the_brand_name_or_component_title(db_session, monkeypatch):
    # An unusual, recognizable component title -- if it (or the brand name) leaks into the bridge
    # text, the deterministic repair pass must catch and rewrite it before the customer sees it.
    unusual_title = "Midnight Saffron Reserve"
    recommendation_id = f"pytest-rec-leak-{uuid.uuid4().hex[:8]}"
    conversation_id = _conversation_id("leak")
    db_session.add(FragranceRecommendation(
        id=recommendation_id, conversationId=conversation_id, createdAt=utcnow(),
        customerProfileJson={}, productsJson=[{"title": unusual_title, "notes": [], "contribution": "anchor"}],
        combinationType="HYBRID", scoreJson={}, evidenceJson={}, ratiosJson=[{"productTitle": unusual_title, "ratioPercent": 100}],
        customerFacingJson={"customerFacingName": "Custom Blend"}, status="pending", buildStatus="draft",
    ))
    await db_session.commit()
    calls = []

    async def _fake_call_openai_once(messages, use_tools, tool_choice=None):
        if tool_choice:
            return {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}  # extraction pre-pass
        calls.append(use_tools)
        if len(calls) == 1:
            # Simulated leak: the bridge names the brand and the real component title (which the
            # model could only have guessed -- it is no longer in its context).
            return {"choices": [{"finish_reason": "stop", "message": {
                "content": f"I combined {unusual_title} with a DUA classic for a bold, warm direction.",
            }}]}
        return {"choices": [{"finish_reason": "stop", "message": {
            "content": "I kept it bold and warm, with real depth that lingers through the day.",
        }}]}

    async def _fake_pipeline(session, cid, context):
        from app.services.recommendation_pipeline import PipelineOutcome
        from app.ai.safe_views import CustomerSafeRecommendation

        return PipelineOutcome(status="READY", recommendation_id=recommendation_id, preview_url="https://example.test/preview", safe_recommendation=CustomerSafeRecommendation(name="Custom Blend"))

    monkeypatch.setattr(conversation_flow, "call_openai_once", _fake_call_openai_once)
    monkeypatch.setattr(conversation_flow, "should_generate", lambda cid, profile, mode: True)
    monkeypatch.setattr(conversation_flow, "run_private_recommendation", _fake_pipeline)

    try:
        history = [{"role": "user", "content": "I need something bold for my wedding"}]
        result = await call_ai(db_session, history, conversation_id, None, None, SHOP_DOMAIN)

        assert unusual_title.lower() not in result["replyText"].lower()
        assert "dua" not in result["replyText"].lower()
        assert result["replyText"] == "I kept it bold and warm, with real depth that lingers through the day."
    finally:
        await db_session.execute(delete(FragranceRecommendation).where(FragranceRecommendation.id == recommendation_id))
        await db_session.execute(delete(CustomerProfileState).where(CustomerProfileState.conversationId == conversation_id))
        await db_session.commit()


async def test_general_reply_mentioning_brand_name_gets_repaired(db_session, monkeypatch):
    calls = []

    async def _fake_call_openai_once(messages, use_tools, tool_choice=None):
        if tool_choice:
            return {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}
        calls.append(use_tools)
        if len(calls) == 1:
            return {"choices": [{"finish_reason": "stop", "message": {"content": "Welcome! I'm happy to help you find a DUA fragrance today."}}]}
        return {"choices": [{"finish_reason": "stop", "message": {"content": "Welcome! I'm happy to help you find a fragrance today."}}]}

    monkeypatch.setattr(conversation_flow, "call_openai_once", _fake_call_openai_once)
    conversation_id = _conversation_id("brand-leak-general")
    history = [{"role": "user", "content": "hi"}]
    result = await call_ai(db_session, history, conversation_id, None, None, SHOP_DOMAIN)
    assert "dua" not in result["replyText"].lower()


# ---------------------------------------------------------------------------
# Same-turn identity refresh -- tool_context's customerName/customerEmail were snapshotted once
# before the tool-resolution loop started. A profile-mutating tool (save_customer_profile_field)
# can persist a newly-revealed name DURING that same loop; a later tool call in the same turn
# (recommendation generation's identity preflight in particular) must see it immediately, not on
# the customer's next message. Verified live: a customer whose name arrived on the exact turn
# generation was attempted got an incorrect "please sign in" message that only cleared up once
# call_ai re-fetched the profile from scratch on their NEXT turn.
# ---------------------------------------------------------------------------

def _ready_profile_fields() -> dict:
    # Every get_missing_required_fields dimension covered except name, which each test sets up
    # deliberately -- matches the exact live scenario (fully ready to generate the moment identity
    # resolves).
    return {
        "city": "Los Angeles", "country": "United States", "locationVerified": True, "locationSource": "order_history",
        "occasionAsked": True, "dislikesAsked": True, "strengthPreference": "moderate", "likes": ["Fruity"],
        "customBuildAccepted": True,
    }


async def test_name_revealed_and_generation_attempted_in_the_same_turn_succeeds(db_session, monkeypatch):
    """Case 1: known email present, profile name initially missing, customer gives their name this
    turn, and the model tries to generate in the very same reply (two tool_calls in one model
    response -- the exact shape of the live regression)."""
    conversation_id = _conversation_id("same-turn-identity-ok")
    known_email = "haseeb@example.test"
    try:
        await save_customer_profile_fields(db_session, conversation_id, _ready_profile_fields())
        calls = []

        async def _fake_call_openai_once(messages, use_tools, tool_choice=None):
            if tool_choice:
                return {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}
            calls.append(use_tools)
            if len(calls) == 1:
                # The model saves the name; Phase 3: the server notices the profile is now
                # complete and runs the private pipeline itself in this same turn.
                return {"choices": [{"finish_reason": "tool_calls", "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "call_name", "function": {"name": "save_customer_profile_field", "arguments": json.dumps({"field": "name", "value": "Haseeb"})}},
                    ],
                }}]}
            return {"choices": [{"finish_reason": "stop", "message": {"content": "Here's what I put together for you."}}]}

        monkeypatch.setattr(conversation_flow, "call_openai_once", _fake_call_openai_once)
        history = [{"role": "assistant", "content": "What should I call you?"}, {"role": "user", "content": "Haseeb"}]
        result = await call_ai(db_session, history, conversation_id, known_email, None, SHOP_DOMAIN)

        assert "sign" not in result["replyText"].lower()
        # The server ran the private pipeline in this same turn: either a fragrance was built
        # (preview_ready) or, without catalog data, a status label reached the model. Never an
        # identity complaint, which is the live bug this guards.
        server_ran = any(e.get("type") == "preview_ready" for e in result["sseEvents"]) or any(
            m.get("role") == "tool" and '"status"' in (m.get("content") or "") and "IDENTITY_NEEDED" not in m["content"] for m in result["updatedMessages"]
        )
        assert server_ran
        profile = await get_customer_profile(db_session, conversation_id)
        assert profile["name"] == "Haseeb"
        assert profile["email"] == known_email
    finally:
        await db_session.execute(delete(FragranceRecommendation).where(FragranceRecommendation.conversationId == conversation_id))
        await _cleanup(db_session, conversation_id)


async def test_name_only_without_email_still_requires_identity(db_session, monkeypatch):
    """Case 3: identity is still genuinely incomplete without an email -- the fix must never
    weaken that requirement, only correct the stale-snapshot timing bug."""
    conversation_id = _conversation_id("same-turn-identity-no-email")
    try:
        await save_customer_profile_fields(db_session, conversation_id, _ready_profile_fields())
        calls = []

        async def _fake_call_openai_once(messages, use_tools, tool_choice=None):
            if tool_choice:
                return {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}
            calls.append(use_tools)
            if len(calls) == 1:
                return {"choices": [{"finish_reason": "tool_calls", "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "call_name", "function": {"name": "save_customer_profile_field", "arguments": json.dumps({"field": "name", "value": "Haseeb"})}},
                    ],
                }}]}
            return {"choices": [{"finish_reason": "stop", "message": {"content": "Here's what I put together for you."}}]}

        monkeypatch.setattr(conversation_flow, "call_openai_once", _fake_call_openai_once)
        history = [{"role": "assistant", "content": "What should I call you?"}, {"role": "user", "content": "Haseeb"}]
        result = await call_ai(db_session, history, conversation_id, None, None, SHOP_DOMAIN)

        # Phase 3: the server pipeline ran (profile complete) but the identity gate refused;
        # the model receives only the IDENTITY_NEEDED status label, never a reason string.
        tool_messages = [m for m in result["updatedMessages"] if m.get("role") == "tool"]
        status_result = next(m["content"] for m in tool_messages if '"status"' in m["content"])
        assert json.loads(status_result)["status"] == "IDENTITY_NEEDED"
        assert not any(e.get("type") == "preview_ready" for e in result["sseEvents"])
        profile = await get_customer_profile(db_session, conversation_id)
        assert profile["name"] == "Haseeb"
        assert not profile.get("email")
    finally:
        await db_session.execute(delete(FragranceRecommendation).where(FragranceRecommendation.conversationId == conversation_id))
        await _cleanup(db_session, conversation_id)


async def test_known_name_and_known_email_generates_normally(db_session, monkeypatch):
    """Case 2: both already known from the account -- ordinary, unaffected path."""
    conversation_id = _conversation_id("same-turn-identity-both-known")
    try:
        await save_customer_profile_fields(db_session, conversation_id, _ready_profile_fields())

        async def _fake_call_openai_once(messages, use_tools, tool_choice=None):
            if tool_choice:
                return {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}
            return {"choices": [{"finish_reason": "stop", "message": {"content": "Here's what I put together for you."}}]}

        monkeypatch.setattr(conversation_flow, "call_openai_once", _fake_call_openai_once)
        history = [{"role": "assistant", "content": "Shall I put the blend together for you?"}, {"role": "user", "content": "let's do it"}]
        result = await call_ai(db_session, history, conversation_id, "haseeb@example.test", "Haseeb", SHOP_DOMAIN)

        assert "sign" not in result["replyText"].lower()
        # Server-triggered: a fragrance (preview_ready) or, without catalog data, a non-identity status.
        assert any(e.get("type") == "preview_ready" for e in result["sseEvents"]) or any(
            m.get("role") == "tool" and '"status"' in (m.get("content") or "") and "IDENTITY_NEEDED" not in m["content"] for m in result["updatedMessages"]
        )
    finally:
        await db_session.execute(delete(FragranceRecommendation).where(FragranceRecommendation.conversationId == conversation_id))
        await _cleanup(db_session, conversation_id)


async def test_location_verified_earlier_in_the_same_loop_is_used_by_generation(db_session, monkeypatch):
    """Case 5: verify_customer_location earlier in the same tool loop, generation later in the
    same loop -- confirms this was already correct (each recommendation handler re-fetches the
    profile from the database itself rather than reading a stale in-memory snapshot) and stays
    that way. Not a new fix -- a protective regression test for the mechanism this task's fix
    generalizes from."""
    conversation_id = _conversation_id("same-turn-location-fresh")
    try:
        await save_customer_profile_fields(db_session, conversation_id, {
            "occasionAsked": True, "dislikesAsked": True, "strengthPreference": "moderate", "likes": ["Fruity"],
            "customBuildAccepted": True, "nameAsked": True,
        })

        async def _fake_call_openai_once(messages, use_tools, tool_choice=None):
            if tool_choice:
                return {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}
            if not any(m.get("role") == "tool" and "Verified" in (m.get("content") or "") for m in messages):
                return {"choices": [{"finish_reason": "tool_calls", "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "call_location", "function": {"name": "verify_customer_location", "arguments": json.dumps({"cityText": "Los Angeles"})}},
                    ],
                }}]}
            return {"choices": [{"finish_reason": "stop", "message": {"content": "Here's what I put together for you."}}]}

        # Phase 5A: this test used to reach the REAL geocoding and weather services (it passed only
        # while the network was reachable). The default-deny network guard exposed that. The
        # location service is now mocked with a synthetic verified result; the subject of the
        # test (a profile written earlier in the loop is re-read by generation) is unchanged.
        from app.ai import tool_executor as _tool_executor

        async def _verified_city(session, city_text):
            return {"verified": True, "city": "Los Angeles", "stateRegion": "California", "country": "United States", "latitude": 34.05, "longitude": -118.24,
                    "source": "synthetic", "needsClarification": False, "candidates": []}

        async def _no_weather(*a, **kw):
            return None

        monkeypatch.setattr(_tool_executor, "verify_city", _verified_city)
        monkeypatch.setattr(_tool_executor, "fetch_current_weather", _no_weather)
        monkeypatch.setattr(conversation_flow, "call_openai_once", _fake_call_openai_once)
        history = [{"role": "assistant", "content": "Which city are you in?"}, {"role": "user", "content": "Los Angeles"}]
        result = await call_ai(db_session, history, conversation_id, "haseeb@example.test", "Haseeb", SHOP_DOMAIN)

        assert "not enough signal" not in result["replyText"].lower()
        profile = await get_customer_profile(db_session, conversation_id)
        assert profile["locationVerified"] is True
        assert profile["city"].lower() == "los angeles"
    finally:
        await db_session.execute(delete(FragranceRecommendation).where(FragranceRecommendation.conversationId == conversation_id))
        await _cleanup(db_session, conversation_id)
