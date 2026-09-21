"""Phase 2 regression tests for F6: bounded model context, bounded tool loop, bounded output
tokens, and a turn deadline. No live OpenAI; no database (the DB touch points inside call_ai are
stubbed)."""

import json

import pytest

from app.ai import conversation_flow
from app.ai.conversation_flow import call_ai
from app.ai.model_context import select_model_context
from app.config import settings
from app.services import copy_generation


# ---------------------------------------------------------------------------
# select_model_context
# ---------------------------------------------------------------------------

def _user(i, size=10):
    return {"role": "user", "content": f"u{i}:" + "x" * size}


def test_context_keeps_only_the_most_recent_messages_within_both_budgets():
    history = [_user(i) for i in range(100)]
    window = select_model_context(history, max_messages=5, max_chars=10_000)
    assert [m["content"][:3] for m in window] == ["u95", "u96", "u97", "u98", "u99"]
    window = select_model_context(history, max_messages=100, max_chars=45)  # 13 chars each -> 3 fit
    assert len(window) == 3 and window[-1] is history[-1]


def test_context_never_starts_with_orphaned_tool_messages():
    history = [
        _user(0),
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
        {"role": "tool", "tool_call_id": "c2", "content": "result"},
        {"role": "assistant", "content": "reply"},
        _user(1),
    ]
    window = select_model_context(history, max_messages=4, max_chars=10_000)
    assert window[0]["role"] != "tool"
    assert [m["role"] for m in window] == ["assistant", "user"]


def test_context_with_giant_history_stays_bounded_and_history_is_untouched():
    history = [_user(i, size=2000) for i in range(500)]  # ~1 MB
    window = select_model_context(history, max_messages=settings.chat_context_max_messages, max_chars=settings.chat_context_max_chars)
    assert len(window) <= settings.chat_context_max_messages
    assert sum(len(m["content"]) for m in window) <= settings.chat_context_max_chars
    assert len(history) == 500


def test_empty_or_zero_budgets():
    assert select_model_context([], max_messages=5, max_chars=100) == []
    assert select_model_context([_user(1)], max_messages=0, max_chars=100) == []


# ---------------------------------------------------------------------------
# call_ai: what actually reaches the model
# ---------------------------------------------------------------------------

@pytest.fixture
def stubbed_flow(monkeypatch):
    async def _profile(session, cid):
        return {"name": "Sam", "email": None, "likes": [], "dislikes": [], "city": None, "locationVerified": False}

    async def _save_field(session, cid, field, value):
        return await _profile(session, cid)

    async def _build_prompt(session, history, cid, email, name, **kw):
        return "SYSTEM PROMPT"

    monkeypatch.setattr(conversation_flow, "get_customer_profile", _profile)
    monkeypatch.setattr(conversation_flow, "build_system_prompt", _build_prompt)
    monkeypatch.setattr(conversation_flow, "get_missing_required_fields", lambda p: [])
    import app.ai.prompt as prompt_module

    monkeypatch.setattr(prompt_module, "get_customer_profile", _profile)
    monkeypatch.setattr(prompt_module, "save_customer_profile_field", _save_field)


async def test_giant_stored_history_does_not_produce_giant_model_context(stubbed_flow, monkeypatch):
    monkeypatch.setattr(settings, "chat_context_max_messages", 6)
    monkeypatch.setattr(settings, "chat_context_max_chars", 5000)
    seen = []

    async def _fake(messages, tools, tool_choice=None):
        seen.append(list(messages))  # snapshot: the flow appends its reply to the same list later
        if tool_choice:
            return {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}
        return {"choices": [{"finish_reason": "stop", "message": {"content": "reply"}}]}

    monkeypatch.setattr(conversation_flow, "call_openai_once", _fake)
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i} " + "y" * 900} for i in range(300)]
    history.append({"role": "user", "content": "I need something fresh for my wedding"})
    result = await call_ai(None, history, "conv-ctx", None, None, "test-shop.myshopify.com")
    main_call = [m for m in seen if m[0]["content"] == "SYSTEM PROMPT"][0]
    # Exclude the system prompt and the Phase 3 customer-context data pair (assistant tool call +
    # tool result), which are not history.
    non_system = [m for m in main_call if m["role"] != "system" and not (m.get("tool_calls") and m["tool_calls"][0]["function"]["name"] == "load_customer_context") and not (m["role"] == "tool" and '"customerContext"' in (m.get("content") or ""))]
    assert len(non_system) <= 6
    assert sum(len(m.get("content") or "") for m in non_system) <= 5000
    assert non_system[-1]["content"] == "I need something fresh for my wedding"
    # Stored history is untouched and the turn's additions are appended to it, never replacing it.
    assert len(history) == 301
    assert len(result["updatedMessages"]) >= 302 and result["updatedMessages"][:301] == history


async def test_tool_loop_is_bounded_by_settings(stubbed_flow, monkeypatch):
    monkeypatch.setattr(settings, "chat_max_tool_turns", 3)
    monkeypatch.setattr(settings, "chat_max_tool_calls_per_turn", 2)
    executed = []
    completions = []

    async def _always_tool_calls(messages, tools, tool_choice=None):
        if tool_choice:
            return {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}
        completions.append(1)
        return {"choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "resolve_season_preference", "arguments": json.dumps({"choice": "keep_style"})}} for i in range(5)
        ]}}]}

    async def _execute(session, tool_name, args, context, **kw):
        executed.append(tool_name)
        return {"modelContent": "resolved", "sseEvent": None}

    monkeypatch.setattr(conversation_flow, "call_openai_once", _always_tool_calls)
    monkeypatch.setattr(conversation_flow, "execute_model_tool", _execute)
    history = [{"role": "user", "content": "I need something fresh for my wedding"}]
    result = await call_ai(None, history, "conv-loop", None, None, "test-shop.myshopify.com")
    assert len(completions) == 3          # never a 4th model call
    assert len(executed) == 3 * 2         # 5 requested per turn, 2 executed
    assert result["replyText"]


def test_output_token_ceilings_are_configured_and_sent(monkeypatch):
    assert settings.openai_max_output_tokens > 0 and settings.openai_copy_max_output_tokens > 0
    import httpx

    from app.ai import openai_client

    captured = {}

    async def _fake_post(self, url, json=None, headers=None, **kw):
        captured["payload"] = json
        return httpx.Response(200, request=httpx.Request("POST", url), json={"choices": [{"message": {"content": "x"}}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
    import asyncio

    asyncio.run(openai_client.call_openai_once([{"role": "user", "content": "hi"}], None))
    assert captured["payload"]["max_tokens"] == settings.openai_max_output_tokens


def test_copy_model_call_has_an_output_ceiling(monkeypatch):
    import asyncio

    import httpx

    captured = {}

    async def _fake_post(url, payload, headers):
        captured["payload"] = payload
        return httpx.Response(200, request=httpx.Request("POST", url), json={"choices": [{"message": {"content": json.dumps({"description": "a", "whySuits": "b"})}}]})

    monkeypatch.setattr(copy_generation, "_http_post", _fake_post)
    monkeypatch.setattr(settings, "openai_api_key", "fake")
    asyncio.run(copy_generation.call_copy_model([{"role": "user", "content": "x"}]))
    assert captured["payload"]["max_tokens"] == settings.openai_copy_max_output_tokens


def test_turn_deadline_is_configured_and_bounded():
    assert 10 <= settings.chat_turn_deadline_seconds <= 300
    assert settings.chat_max_tool_turns <= 20 and settings.chat_max_tool_calls_per_turn <= 10
