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
    extract_email_from_history,
    get_known_profile_field_names,
)
from app.ai.tool_executor import execute_fragrance_tool
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

    system_prompt = await build_system_prompt(session, history, conversation_id, known_customer_email, known_customer_name)
    messages: list[dict] = [{"role": "system", "content": system_prompt}, *history]
    final_text = ""
    sse_events: list[dict] = []
    called_tool_names: list[str] = []
    tool_context = {
        "conversationId": conversation_id, "customerName": confirmed_customer_name,
        "customerEmail": confirmed_customer_email, "shopDomain": shop_domain,
    }

    # Up to 10 tool-resolution turns -- 6 wasn't enough headroom for the model to save several
    # profile fields one at a time (it doesn't batch parallel tool calls) and still reach
    # analyze_customer_product_candidates/generate_new_product_combinations in the same turn.
    for turn in range(10):
        data = await call_openai_once(messages, True)
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
                bridge_data = await call_openai_once(messages, False)
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
