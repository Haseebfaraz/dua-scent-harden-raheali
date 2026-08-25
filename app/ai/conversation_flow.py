"""Port of app/routes/chat.jsx's conversation memory (section 2) and callAI (section 5) -- the
6-turn tool-resolution loop that drives one chat turn end to end.
"""

import json
import logging
import re
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.openai_client import call_openai_once
from app.ai.prompt import (
    build_system_prompt,
    count_assistant_question_turns,
    detect_high_signal_flags,
    determine_conversation_mode,
    extract_email_from_history,
    get_known_profile_field_names,
)
from app.ai.tool_executor import execute_fragrance_tool
from app.ai.tools import EXTRACTABLE_PROFILE_FIELD_NAMES, FRAGRANCE_AGENT_TOOLS, GENERAL_CONVERSATION_TOOLS, PROFILE_EXTRACTION_TOOL
from app.services.customer_profile import get_customer_profile, get_missing_required_fields
from app.services.conversation import get_conversation_history

logger = logging.getLogger(__name__)

# CONVERSATIONS is wiped on every process restart even though every message is also durably saved
# to Postgres via save_message -- when a returning customer's id isn't in memory, this rehydrates
# from the DB instead of silently starting a blank conversation.
_CONVERSATIONS: dict[str, list[dict]] = {}


async def get_conversation(session: AsyncSession, conversation_id: str | None) -> dict[str, Any]:
    if conversation_id and conversation_id in _CONVERSATIONS:
        return {"id": conversation_id, "history": _CONVERSATIONS[conversation_id]}

    if conversation_id:
        db_messages = await get_conversation_history(session, conversation_id)
        if db_messages:
            history = [{"role": m.role, "content": m.content} for m in db_messages]
            _CONVERSATIONS[conversation_id] = history
            return {"id": conversation_id, "history": history}

    new_id = str(uuid.uuid4())
    _CONVERSATIONS[new_id] = []
    return {"id": new_id, "history": _CONVERSATIONS[new_id]}


def set_conversation_cache(conversation_id: str, history: list[dict]) -> None:
    _CONVERSATIONS[conversation_id] = history


_LEAKED_ID_PATTERN = re.compile(r"\bc[a-z0-9]{20,}\b", re.IGNORECASE)


async def _extract_and_persist_profile_facts(
    session: AsyncSession, history: list[dict], conversation_id: str, tool_context: dict,
) -> tuple[list[dict], list[dict]]:
    """One forced, structured extraction call up front instead of the model spending one full
    round trip per fact via repeated save_customer_profile_field calls -- verified live that a
    single fact-dense message ("strong and woody for date night, I'm in LA, love oud and hate
    vanilla") was producing 5+ sequential save_customer_profile_field round trips, one per field.

    Persists through the exact same execute_fragrance_tool dispatch the model itself would
    otherwise use, so validation, vocabulary correction, dislike filtering, and location/weather
    verification behave identically either way -- this only changes how many round trips it takes
    to get there, never what gets saved or how.

    Returns (sse_events, synthetic_messages). The synthetic messages matter: a static "profile
    already saved: {...}" line in the system prompt was NOT enough on its own -- verified live
    that the main loop's model still re-derived and re-saved (and even re-verified location)
    everything from the raw customer text anyway, since it had no actual memory of having just
    "done" anything (a prose claim doesn't carry the weight of the model's own tool-call history).
    Synthesizing the exact assistant/tool_calls + tool-result messages this extraction performed,
    appended after the customer's message, gives the main loop real conversational memory that it
    already acted on this message -- the same mechanism it already trusts for its own prior turns.

    Purely additive: on any failure (network, parse, empty result) this just no-ops and the normal
    tool loop below still catches anything missed, exactly as it did before this existed.
    """
    latest_user_message = next((m for m in reversed(history) if m.get("role") == "user"), None)
    if not latest_user_message:
        return [], []

    profile = await get_customer_profile(session, conversation_id)
    extraction_system_prompt = (
        "You are extracting structured fragrance-profile facts from this conversation's most "
        "recent customer message. This is not a reply to the customer -- you never write "
        "conversational text here, only call record_profile_updates with what you found.\n\n"
        f"Profile already saved (do not re-extract anything already set here): {json.dumps(profile)}\n\n"
        "Extract only from the customer's most recent message below; earlier turns are context "
        "for disambiguation only (e.g. a bare \"no\" answering \"any dislikes?\" means "
        "dislikesAsked=true, not a literal dislike named \"no\")."
    )
    extraction_messages = [{"role": "system", "content": extraction_system_prompt}, *history[-6:]]

    data = await call_openai_once(
        extraction_messages, [PROFILE_EXTRACTION_TOOL],
        tool_choice={"type": "function", "function": {"name": "record_profile_updates"}},
    )
    if not data:
        return [], []

    try:
        tool_calls = data["choices"][0]["message"].get("tool_calls") or []
        if not tool_calls:
            return [], []
        args = json.loads(tool_calls[0]["function"]["arguments"])
    except (KeyError, IndexError, json.JSONDecodeError, TypeError) as err:
        logger.warning("PROFILE_EXTRACTION_PARSE_FAILED conversationId=%s error=%s", conversation_id, err)
        return [], []

    sse_events: list[dict] = []
    synthetic_calls: list[dict] = []
    synthetic_results: list[dict] = []

    async def _run(tool_name: str, tool_args: dict) -> None:
        call_id = f"extract_{uuid.uuid4().hex[:12]}"
        synthetic_calls.append({"id": call_id, "type": "function", "function": {"name": tool_name, "arguments": json.dumps(tool_args)}})
        result = await execute_fragrance_tool(session, tool_name, json.dumps(tool_args), tool_context)
        synthetic_results.append({"role": "tool", "tool_call_id": call_id, "content": result["modelContent"]})
        if result.get("sseEvent"):
            sse_events.append(result["sseEvent"])

    for update in (args.get("fieldsToUpdate") or []):
        field = update.get("field")
        if field not in EXTRACTABLE_PROFILE_FIELD_NAMES:
            continue
        await _run("save_customer_profile_field", {"field": field, "value": update.get("value")})

    city_text = args.get("cityText")
    if city_text:
        await _run("verify_customer_location", {"cityText": city_text})

    if not synthetic_calls:
        return sse_events, []

    synthetic_messages = [{"role": "assistant", "content": None, "tool_calls": synthetic_calls}, *synthetic_results]
    return sse_events, synthetic_messages


async def call_ai(
    session: AsyncSession, history: list[dict], conversation_id: str,
    known_customer_email: str | None, known_customer_name: str | None, shop_domain: str,
) -> dict[str, Any]:
    if not _has_openai_key():
        return {"replyText": "Configuration error: missing API key.", "sseEvents": []}

    profile_for_identity = await get_customer_profile(session, conversation_id)
    confirmed_customer_name = known_customer_name or profile_for_identity.get("name")
    confirmed_customer_email = known_customer_email or profile_for_identity.get("email") or extract_email_from_history(history)
    profiling_question_count_before = count_assistant_question_turns(history)
    latest_user_message = next((m for m in reversed(history) if m.get("role") == "user"), None)
    high_signal_flags = detect_high_signal_flags((latest_user_message or {}).get("content") or "")
    missing_required_fields_before = get_missing_required_fields(profile_for_identity)

    logger.info("CHAT_PROFILE_STATE %s", json.dumps({
        "conversationId": conversation_id,
        "profilingQuestionCount": profiling_question_count_before,
        "knownProfileFields": get_known_profile_field_names(profile_for_identity),
        "missingRequiredFieldCount": len(missing_required_fields_before),
        "missingRequiredFields": missing_required_fields_before,
        "highSignalFlags": high_signal_flags,
    }))

    tool_context = {
        "conversationId": conversation_id, "customerName": confirmed_customer_name,
        "customerEmail": confirmed_customer_email, "shopDomain": shop_domain,
    }

    # Deterministic, not left to the model: while conversationMode is GENERAL_CONVERSATION the
    # fragrance-discovery tools (analyze/generate/verify_location/etc.) are not even offered, so a
    # fragrance question or an analysis call during small talk is structurally impossible, not just
    # discouraged by prompt wording (verified live that wording alone was not reliable at
    # temperature > 0). save_customer_profile_field stays available so a volunteered name/email can
    # still be saved.
    conversation_mode = determine_conversation_mode(history, profile_for_identity)
    tools_for_turn = GENERAL_CONVERSATION_TOOLS if conversation_mode == "GENERAL_CONVERSATION" else FRAGRANCE_AGENT_TOOLS

    # Batch-extract before building the system prompt, so profile_status_line/missing_fields below
    # already reflect whatever this message just supplied -- collapses what used to be several
    # sequential save_customer_profile_field round trips into one.
    sse_events: list[dict] = []
    extraction_synthetic_messages: list[dict] = []
    if conversation_mode == "FRAGRANCE_DISCOVERY":
        extracted_sse_events, extraction_synthetic_messages = await _extract_and_persist_profile_facts(session, history, conversation_id, tool_context)
        sse_events.extend(extracted_sse_events)

    system_prompt = await build_system_prompt(session, history, conversation_id, known_customer_email, known_customer_name)
    # The synthetic tool-call/result pair goes AFTER the customer's message so the model sees it as
    # its own completed reaction to that message -- a static "already saved" line in the prompt was
    # not enough on its own to stop the model re-deriving and re-saving everything itself.
    messages: list[dict] = [{"role": "system", "content": system_prompt}, *history, *extraction_synthetic_messages]
    final_text = ""
    called_tool_names: list[str] = []

    # Up to 10 tool-resolution turns -- 6 wasn't enough headroom for the model to save several
    # profile fields one at a time (it doesn't batch parallel tool calls) and still reach
    # analyze_customer_product_candidates/generate_new_product_combinations in the same turn.
    for turn in range(10):
        data = await call_openai_once(messages, tools_for_turn)
        if not data:
            return {"replyText": "Sorry, I'm having trouble reaching the fragrance engine right now.", "sseEvents": sse_events}

        choice = data["choices"][0]
        message = choice["message"]
        tool_calls = message.get("tool_calls")

        if choice.get("finish_reason") == "tool_calls" and tool_calls:
            messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": tool_calls})

            preview_ready = False
            for tool_call in tool_calls:
                called_tool_names.append(tool_call["function"]["name"])
                result = await execute_fragrance_tool(session, tool_call["function"]["name"], tool_call["function"]["arguments"], tool_context)
                if result.get("sseEvent"):
                    sse_events.append(result["sseEvent"])

                messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": result["modelContent"]})

                if (result.get("sseEvent") or {}).get("type") == "preview_ready":
                    preview_ready = True
                    break
            if preview_ready:
                # The tool-call turn itself carried no text (the model spent its turn calling the
                # tool) -- one more completion, tools disabled, lets it actually write the
                # grounded reasoning bridge the tool result just asked for, instead of a canned
                # line that ignores what the customer said and what got selected.
                bridge_data = await call_openai_once(messages, None)
                bridge_message = (bridge_data["choices"][0]["message"] if bridge_data else {})
                final_text = bridge_message.get("content") or "I've got the blend ready — take a look."
                messages.append({"role": "assistant", "content": final_text})
                logger.info("CHAT_NEXT_ACTION %s", json.dumps({
                    "conversationId": conversation_id, "action": "GENERATE", "reason": "preview_ready",
                    "calledTools": called_tool_names, "profilingQuestionCountBefore": profiling_question_count_before,
                    "profilingQuestionCountAfter": profiling_question_count_before,
                }))
                persisted_messages = [m for m in messages if m.get("role") != "system"]
                return {"replyText": final_text, "sseEvents": sse_events, "updatedMessages": persisted_messages}
            continue

        final_text = message.get("content") or ""

        # A deterministic safety net for a leaked internal technical identifier (Prisma-style
        # cuid, e.g. recommendationId) -- a long lowercase alphanumeric token starting with "c"
        # doesn't occur in ordinary English, so this only ever fires on an actual leaked ID.
        leaked_id = turn < 5 and bool(_LEAKED_ID_PATTERN.search(final_text))
        if leaked_id:
            messages.append({"role": "assistant", "content": final_text})
            messages.append({
                "role": "system",
                "content": "CRITICAL: your last reply contained what looks like an internal database identifier — customers must NEVER see this. Rewrite that reply now without any technical ID.",
            })
            continue

        messages.append({"role": "assistant", "content": final_text})
        break

    asked_question_this_turn = isinstance(final_text, str) and "?" in final_text
    generated_recommendation = any(n in ("generate_new_product_combinations", "refine_combination_recommendations") for n in called_tool_names)
    updated_profile = any(n in ("save_customer_profile_field", "verify_customer_location", "resolve_season_preference") for n in called_tool_names)

    next_action = (
        "GENERATE" if generated_recommendation
        else ("FOLLOW_UP" if (asked_question_this_turn and high_signal_flags) else "ASK") if asked_question_this_turn
        else "PROFILE_UPDATE" if updated_profile
        else "CONVERSATION"
    )
    action_reason = (
        "recommendation_tools_called" if generated_recommendation
        else "high_signal_follow_up_or_clarification" if (asked_question_this_turn and high_signal_flags)
        else "missing_or_useful_information" if asked_question_this_turn
        else "profile_fact_captured" if updated_profile
        else "no_question_needed"
    )
    logger.info("CHAT_NEXT_ACTION %s", json.dumps({
        "conversationId": conversation_id, "action": next_action, "reason": action_reason,
        "calledTools": called_tool_names, "highSignalFlags": high_signal_flags,
        "profilingQuestionCountBefore": profiling_question_count_before,
        "profilingQuestionCountAfter": profiling_question_count_before + (1 if asked_question_this_turn else 0),
    }))

    persisted_messages = [m for m in messages if m.get("role") != "system"]
    return {"replyText": final_text or "Let's get that crafted for you.", "sseEvents": sse_events, "updatedMessages": persisted_messages}


def _has_openai_key() -> bool:
    from app.config import settings

    return bool(settings.openai_api_key)
