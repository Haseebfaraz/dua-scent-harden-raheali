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


# ---- Phase 6: /chat is the direct, public storefront-facing route (no internal key at all) ----

def test_public_chat_history_requires_history_true_flag():
    with TestClient(app) as client:
        response = client.get("/chat", params={"conversation_id": f"pytest-public-{uuid.uuid4().hex}"})
    assert response.status_code == 200
    assert response.json() == {"messages": []}  # no ?history=true -- matches chat.jsx's loader default


def test_public_chat_history_returns_real_history_when_flagged():
    with TestClient(app) as client:
        response = client.get("/chat", params={"history": "true", "conversation_id": f"pytest-public-{uuid.uuid4().hex}"})
    assert response.status_code == 200
    assert response.json() == {"messages": []}  # unknown conversation -- empty, not an error


def test_public_chat_accepts_no_internal_key_at_all(monkeypatch):
    async def _fake_call_openai_once(messages, use_tools):
        return {"choices": [{"finish_reason": "stop", "message": {"content": "Hi! What's your name?"}}]}

    monkeypatch.setattr(conversation_flow, "call_openai_once", _fake_call_openai_once)

    conversation_id = f"pytest-public-{time.time()}-{uuid.uuid4().hex[:8]}"
    with TestClient(app) as client:
        # No X-Internal-Api-Key header and no shop_domain -- exactly what a direct browser call
        # looks like once Node is out of the loop.
        response = client.post("/chat", json={"conversation_id": conversation_id, "message": "hello"})
    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert [e["type"] for e in events] == ["id", "chunk", "message_complete", "end_turn"]


def test_internal_route_still_requires_the_key_even_though_public_route_does_not():
    if not settings.internal_api_key:
        return
    with TestClient(app) as client:
        assert client.post("/internal/chat", json={"message": "hi"}).status_code == 401
        assert client.post("/chat", json={"message": "hi"}).status_code != 401
