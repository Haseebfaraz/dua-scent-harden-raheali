"""Chat API contract tests.

Phase 2 (security): the public route requires a server-minted conversation capability for every
continuation and history read (see tests/security/test_conversation_ownership.py for the attack
matrix); the internal Node-adapter route requires X-Internal-Api-Key unconditionally. The SSE
frame ordering guarantees from Phase 7B are unchanged."""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.ai import conversation_flow
from app.api import chat as chat_module
from app.config import settings
from app.db.models import Conversation, CustomerProfileState, Message
from app.db.session import SessionLocal
from app.main import app
from app.services.conversation_capability import CONVERSATION_TOKEN_HEADER

SHOP_DOMAIN = "test-shop.myshopify.com"


def _headers():
    return {"X-Internal-Api-Key": settings.internal_api_key}


def _parse_sse(body: str) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]


async def _cleanup(cid: str) -> None:
    async with SessionLocal() as session:
        await session.execute(delete(Message).where(Message.conversationId == cid))
        await session.execute(delete(CustomerProfileState).where(CustomerProfileState.conversationId == cid))
        await session.execute(delete(Conversation).where(Conversation.id == cid))
        await session.commit()


@pytest.fixture(autouse=True)
def _reset_cache():
    conversation_flow._CONVERSATIONS.clear()


def test_history_empty_for_unknown_conversation():
    with TestClient(app) as client:
        response = client.get("/internal/chat/history", params={"conversation_id": f"pytest-chatapi-{uuid.uuid4().hex}"}, headers=_headers())
        assert response.status_code == 200
        assert response.json() == {"messages": []}


def test_history_with_no_conversation_id():
    with TestClient(app) as client:
        response = client.get("/internal/chat/history", headers=_headers())
        assert response.status_code == 200
        assert response.json() == {"messages": []}


def test_internal_routes_reject_missing_or_wrong_secret():
    with TestClient(app) as client:
        assert client.get("/internal/chat/history").status_code == 401
        assert client.get("/internal/chat/history", headers={"X-Internal-Api-Key": "wrong"}).status_code == 401
        assert client.post("/internal/chat", json={"message": "hi"}).status_code == 401


def test_internal_routes_fail_closed_when_no_secret_is_configured(monkeypatch):
    # Phase 2: an unset INTERNAL_API_KEY no longer disables the check.
    monkeypatch.setattr(settings, "internal_api_key", "")
    with TestClient(app) as client:
        assert client.get("/internal/chat/history", headers={"X-Internal-Api-Key": ""}).status_code == 401
        assert client.get("/internal/chat/history").status_code == 401


async def test_post_internal_chat_streams_expected_sse_event_sequence(monkeypatch):
    async def _fake_call_openai_once(messages, use_tools, tool_choice=None):
        if tool_choice:
            return {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}
        return {"choices": [{"finish_reason": "stop", "message": {"content": "Hey there! What should I call you?"}}]}

    monkeypatch.setattr(conversation_flow, "call_openai_once", _fake_call_openai_once)

    with TestClient(app) as client:
        response = client.post("/internal/chat", json={
            "conversation_id": f"pytest-chatapi-{uuid.uuid4().hex[:8]}", "message": "hello",
            "customer_email": "test@example.com", "customer_name": None, "shop_domain": SHOP_DOMAIN,
        }, headers=_headers())
        assert response.status_code == 200
        events = _parse_sse(response.text)
        types = [e["type"] for e in events]
        assert types == ["id", "chunk", "message_complete", "end_turn"]
        # An unknown id is never adopted: the server mints its own and returns it via the "id"
        # event so the adapter stores that one. Internal callers get no capability token.
        assert events[0]["conversation_id"] and "conversation_token" not in events[0]
        assert events[1]["chunk"] == "Hey there! What should I call you?"
    await _cleanup(events[0]["conversation_id"])


async def test_reasoning_bridge_chunk_is_emitted_before_preview_ready(monkeypatch):
    # The widget navigates away the instant it parses a preview_ready frame -- text arriving
    # after that is never seen, so the bridge must come first.
    async def _fake_call_ai(session, history, conversation_id, known_customer_email, known_customer_name, shop_domain, gate=None):
        return {
            "replyText": "You wanted something fresh for the wedding with no oud, so I kept it bright and clean.",
            "sseEvents": [{"type": "preview_ready", "recommendationId": "rec_pytest", "previewId": "rec_pytest", "previewUrl": "https://example.test/preview"}],
            "updatedMessages": history,
        }

    monkeypatch.setattr(chat_module, "call_ai", _fake_call_ai)

    with TestClient(app) as client:
        response = client.post("/internal/chat", json={"message": "I need something fresh for my wedding, no oud", "shop_domain": SHOP_DOMAIN}, headers=_headers())
    assert response.status_code == 200
    events = _parse_sse(response.text)
    types = [e["type"] for e in events]
    chunk_index, preview_ready_index = types.index("chunk"), types.index("preview_ready")
    assert chunk_index < preview_ready_index
    assert events[chunk_index]["chunk"].startswith("You wanted something fresh")
    await _cleanup(events[0]["conversation_id"])


async def test_non_preview_sse_events_keep_their_position_but_are_field_allowlisted(monkeypatch):
    async def _fake_call_ai(session, history, conversation_id, known_customer_email, known_customer_name, shop_domain, gate=None):
        return {
            "replyText": "Got your preferences.",
            "sseEvents": [
                {"type": "profile_progress", "profile": {"email": "jane@example.test"}, "missingFields": ["internal readiness text"]},
                {"type": "candidate_products", "candidateProducts": [{"relevanceScore": 12.5, "sameCityOrders": 3}]},
                {"type": "made_up_event", "secret": "x"},
                {"type": "preview_ready", "recommendationId": "rec_pytest", "previewId": "rec_pytest", "previewUrl": "https://example.test/preview", "internal": "no"},
            ],
            "updatedMessages": history,
        }

    monkeypatch.setattr(chat_module, "call_ai", _fake_call_ai)

    with TestClient(app) as client:
        response = client.post("/internal/chat", json={"message": "hi", "shop_domain": SHOP_DOMAIN}, headers=_headers())
    events = _parse_sse(response.text)
    assert [e["type"] for e in events] == ["id", "profile_progress", "candidate_products", "chunk", "message_complete", "preview_ready", "end_turn"]
    assert events[1] == {"type": "profile_progress"} and events[2] == {"type": "candidate_products"}
    assert "internal" not in events[5] and "jane@example.test" not in response.text and "relevanceScore" not in response.text
    await _cleanup(events[0]["conversation_id"])


# ---- Public route: server-minted session, capability required afterwards ----

async def test_public_chat_bootstraps_without_any_internal_key(monkeypatch):
    async def _fake_call_openai_once(messages, use_tools, tool_choice=None):
        if tool_choice:
            return {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}
        return {"choices": [{"finish_reason": "stop", "message": {"content": "Hi! What's your name?"}}]}

    monkeypatch.setattr(conversation_flow, "call_openai_once", _fake_call_openai_once)

    with TestClient(app) as client:
        response = client.post("/chat", json={"message": "hello"})
        assert response.status_code == 200
        events = _parse_sse(response.text)
        assert [e["type"] for e in events] == ["id", "chunk", "message_complete", "end_turn"]
        cid, token = events[0]["conversation_id"], events[0]["conversation_token"]
        assert client.get("/chat", params={"history": "true", "conversation_id": cid}, headers={CONVERSATION_TOKEN_HEADER: token}).status_code == 200
        assert client.get("/chat", params={"history": "true", "conversation_id": cid}).status_code == 401
        assert client.get("/chat", params={"conversation_id": cid}).json() == {"messages": []}  # no ?history=true -- widget loader default
    await _cleanup(cid)


def test_public_route_ignores_the_internal_key_and_internal_route_still_requires_it():
    with TestClient(app) as client:
        assert client.post("/internal/chat", json={"message": "hi"}).status_code == 401
        assert client.post("/chat", json={"conversation_id": "a" * 32, "message": "hi"}).status_code == 401  # id alone is not authorization
