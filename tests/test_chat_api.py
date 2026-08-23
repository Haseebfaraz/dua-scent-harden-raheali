import time
import uuid

from fastapi.testclient import TestClient

from app.ai import conversation_flow
from app.config import settings
from app.main import app

SHOP_DOMAIN = "test-shop.myshopify.com"
_HEADERS = {"X-Internal-Api-Key": settings.internal_api_key} if settings.internal_api_key else {}


def _parse_sse(body: str) -> list[dict]:
    import json

    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


def test_history_empty_for_unknown_conversation():
    with TestClient(app) as client:
        response = client.get("/internal/chat/history", params={"conversation_id": f"pytest-chatapi-{uuid.uuid4().hex}"}, headers=_HEADERS)
        assert response.status_code == 200
        assert response.json() == {"messages": []}


def test_history_with_no_conversation_id():
    with TestClient(app) as client:
        response = client.get("/internal/chat/history", headers=_HEADERS)
        assert response.status_code == 200
        assert response.json() == {"messages": []}


def test_history_rejects_missing_or_wrong_internal_secret():
    if not settings.internal_api_key:
        return  # nothing to enforce when no secret is configured
    with TestClient(app) as client:
        assert client.get("/internal/chat/history").status_code == 401
        assert client.get("/internal/chat/history", headers={"X-Internal-Api-Key": "wrong"}).status_code == 401


def test_post_chat_streams_expected_sse_event_sequence(monkeypatch):
    async def _fake_call_openai_once(messages, use_tools):
        return {"choices": [{"finish_reason": "stop", "message": {"content": "Hey there! What should I call you?"}}]}

    monkeypatch.setattr(conversation_flow, "call_openai_once", _fake_call_openai_once)

    conversation_id = f"pytest-chatapi-{time.time()}-{uuid.uuid4().hex[:8]}"
    with TestClient(app) as client:
        response = client.post("/internal/chat", json={
            "conversation_id": conversation_id, "message": "hello",
            "customer_email": "test@example.com", "customer_name": None,
            "greeting": None, "shop_domain": SHOP_DOMAIN,
        }, headers=_HEADERS)
        assert response.status_code == 200
        events = _parse_sse(response.text)
        types = [e["type"] for e in events]
        assert types == ["id", "chunk", "message_complete", "end_turn"]
        # A conversation_id with no existing DB history is never preserved as-is -- getConversation
        # mints a fresh id (returned via this "id" event so the client adopts it), exactly like the
        # real chat.jsx: only an id that already has real message history gets rehydrated verbatim.
        assert events[0]["conversation_id"]
        assert events[1]["chunk"] == "Hey there! What should I call you?"


def test_post_chat_rejects_missing_internal_secret():
    if not settings.internal_api_key:
        return
    with TestClient(app) as client:
        response = client.post("/internal/chat", json={"shop_domain": SHOP_DOMAIN, "message": "hi"})
        assert response.status_code == 401
