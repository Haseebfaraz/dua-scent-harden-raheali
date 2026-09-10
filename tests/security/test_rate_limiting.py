"""Phase 2 regression tests for F6: PostgreSQL-backed rate limiting, client-IP trust, and
per-conversation / per-process concurrency. Database-backed (schema-only local Postgres)."""

import asyncio
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.ai import conversation_flow
from app.api import chat as chat_module
from app.api.client_identity import UNKNOWN_CLIENT, client_ip
from app.config import settings
from app.db.models import Conversation, CustomerProfileState, Message, RateLimitBucket
from app.db.session import SessionLocal
from app.db.time import utcnow
from app.main import app
from app.services.conversation_capability import CONVERSATION_TOKEN_HEADER
from app.services.rate_limit import RateLimited, RateLimitUnavailable, enforce, hash_abuse_identity, limit, parse_limit, prune_stale_buckets
from app.services.turn_lock import TurnInProgress, conversation_turn_lock


@pytest.fixture(autouse=True)
def _fake_model(monkeypatch):
    calls = []

    async def _fake(messages, tools, tool_choice=None):
        calls.append(1)
        if tool_choice:
            return {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}
        return {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}

    monkeypatch.setattr(conversation_flow, "call_openai_once", _fake)
    conversation_flow._CONVERSATIONS.clear()
    return calls


@pytest.fixture(autouse=True)
async def _clean_buckets():
    async with SessionLocal() as session:
        await session.execute(delete(RateLimitBucket))
        await session.commit()
    yield
    async with SessionLocal() as session:
        await session.execute(delete(RateLimitBucket))
        await session.commit()


async def _cleanup(cid):
    async with SessionLocal() as session:
        await session.execute(delete(Message).where(Message.conversationId == cid))
        await session.execute(delete(CustomerProfileState).where(CustomerProfileState.conversationId == cid))
        await session.execute(delete(Conversation).where(Conversation.id == cid))
        await session.commit()


# ---------------------------------------------------------------------------
# Limiter primitive
# ---------------------------------------------------------------------------

def test_limit_specs_parse_and_reject_nonsense():
    assert parse_limit("12/60") == (12, 60)
    for bad in ("0/60", "12/0", "12", "a/b"):
        with pytest.raises((ValueError, TypeError)):
            parse_limit(bad)


def test_abuse_identity_is_a_keyed_hash_not_the_ip(monkeypatch):
    monkeypatch.setattr(settings, "abuse_identity_hash_key", "test-key")
    h = hash_abuse_identity("203.0.113.9")
    assert "203.0.113.9" not in h and len(h) == 32
    monkeypatch.setattr(settings, "abuse_identity_hash_key", "other-key")
    assert hash_abuse_identity("203.0.113.9") != h


async def test_fixed_window_counts_deterministically_and_resets():
    subject = f"subj-{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        for _ in range(3):
            await enforce(session, [limit("t", subject, "3/60")])
        with pytest.raises(RateLimited) as info:
            await enforce(session, [limit("t", subject, "3/60")])
        assert 1 <= info.value.retry_after_seconds <= 60
        # Simulate the window rolling over: move the stored window back.
        row = await session.scalar(select(RateLimitBucket).where(RateLimitBucket.key == f"t:{subject}"))
        row.windowStart = row.windowStart - timedelta(seconds=120)
        await session.commit()
        await enforce(session, [limit("t", subject, "3/60")])  # new window, allowed again
        assert "203" not in row.key


async def test_multiple_limits_are_all_counted_and_first_exceeded_wins():
    a, b = f"a-{uuid.uuid4().hex[:6]}", f"b-{uuid.uuid4().hex[:6]}"
    async with SessionLocal() as session:
        await enforce(session, [limit("x", a, "5/60"), limit("y", b, "1/60")])
        with pytest.raises(RateLimited) as info:
            await enforce(session, [limit("x", a, "5/60"), limit("y", b, "1/60")])
        assert info.value.limit_class == "y"


async def test_store_failure_fails_closed():
    class _Broken:
        async def scalar(self, *a, **kw):
            raise RuntimeError("db down")

        async def rollback(self):
            return None

    with pytest.raises(RateLimitUnavailable):
        await enforce(_Broken(), [limit("x", "s", "1/60")])


async def test_prune_removes_only_stale_rows():
    async with SessionLocal() as session:
        session.add(RateLimitBucket(key="old:x", windowStart=utcnow(), count=1, updatedAt=utcnow() - timedelta(days=3)))
        session.add(RateLimitBucket(key="new:x", windowStart=utcnow(), count=1, updatedAt=utcnow()))
        await session.commit()
        removed = await prune_stale_buckets(session)
        assert removed == 1
        assert await session.scalar(select(RateLimitBucket.key).where(RateLimitBucket.key == "new:x")) == "new:x"


# ---------------------------------------------------------------------------
# Client IP trust
# ---------------------------------------------------------------------------

def _request(headers: dict, peer="198.51.100.7"):
    from starlette.requests import Request

    return Request({"type": "http", "method": "GET", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()], "client": (peer, 1234), "query_string": b""})


def test_forwarded_header_is_ignored_with_no_trusted_proxy(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 0)
    assert client_ip(_request({"X-Forwarded-For": "1.2.3.4"})) == "198.51.100.7"


def test_only_the_trusted_proxys_entry_counts(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    # Attacker-supplied entries are to the LEFT of the proxy-appended real client.
    assert client_ip(_request({"X-Forwarded-For": "1.2.3.4, 5.6.7.8, 203.0.113.9"})) == "203.0.113.9"
    assert client_ip(_request({"X-Forwarded-For": "203.0.113.9"})) == "203.0.113.9"
    # No header at all when a proxy is expected: fall back to the socket peer, never trust nothing.
    assert client_ip(_request({})) == "198.51.100.7"
    # Garbage in the trusted slot maps to the strict shared bucket.
    assert client_ip(_request({"X-Forwarded-For": "1.2.3.4, not-an-ip"})) == UNKNOWN_CLIENT


def test_rotating_forged_forwarded_headers_share_one_bucket_when_no_proxy_is_trusted(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 0)
    seen = {client_ip(_request({"X-Forwarded-For": f"10.0.0.{i}"})) for i in range(20)}
    assert seen == {"198.51.100.7"}


# ---------------------------------------------------------------------------
# Endpoint behaviour
# ---------------------------------------------------------------------------

async def test_conversation_creation_is_rate_limited_per_ip_and_never_reaches_the_model(monkeypatch, _fake_model):
    monkeypatch.setattr(settings, "rate_limit_conversation_create_per_ip", "2/3600")
    monkeypatch.setattr(settings, "trusted_proxy_hops", 0)
    created = []
    with TestClient(app) as client:
        for _ in range(2):
            r = client.post("/chat/session", json={})
            assert r.status_code == 200
            created.append(r.json()["conversationId"])
        third = client.post("/chat/session", json={})
        assert third.status_code == 429 and "retry-after" in third.headers
        # Forged X-Forwarded-For does not open a new budget.
        forged = client.post("/chat/session", json={}, headers={"X-Forwarded-For": "8.8.8.8"})
        assert forged.status_code == 429
        # Implicit bootstrap via first message shares the same budget and never hits the model.
        first_message = client.post("/chat", json={"message": "hi"})
        assert first_message.status_code == 429
    assert _fake_model == []
    for cid in created:
        await _cleanup(cid)


async def test_chat_turns_are_rate_limited_per_conversation_before_the_model(monkeypatch, _fake_model):
    monkeypatch.setattr(settings, "rate_limit_chat_turn_per_conversation", "2/60")
    with TestClient(app) as client:
        data = client.post("/chat/session", json={}).json()
        cid, token = data["conversationId"], data["conversationToken"]
        try:
            headers = {CONVERSATION_TOKEN_HEADER: token}
            assert client.post("/chat", json={"conversation_id": cid, "message": "one"}, headers=headers).status_code == 200
            assert client.post("/chat", json={"conversation_id": cid, "message": "two"}, headers=headers).status_code == 200
            model_calls_before = len(_fake_model)
            limited = client.post("/chat", json={"conversation_id": cid, "message": "three"}, headers=headers)
            assert limited.status_code == 429
            assert "retry-after" in limited.headers and limited.headers["cache-control"] == "no-store"
            assert "text/event-stream" not in limited.headers.get("content-type", "")  # failed before the stream
            assert len(_fake_model) == model_calls_before
        finally:
            await _cleanup(cid)


async def test_separate_conversations_still_share_the_per_ip_turn_budget(monkeypatch, _fake_model):
    monkeypatch.setattr(settings, "rate_limit_chat_turn_per_ip", "3/60")
    monkeypatch.setattr(settings, "rate_limit_conversation_create_per_ip", "100/3600")
    cids = []
    with TestClient(app) as client:
        sessions = [client.post("/chat/session", json={}).json() for _ in range(4)]
        cids = [s["conversationId"] for s in sessions]
        try:
            statuses = [client.post("/chat", json={"conversation_id": s["conversationId"], "message": "hi"}, headers={CONVERSATION_TOKEN_HEADER: s["conversationToken"]}).status_code for s in sessions]
            assert statuses == [200, 200, 200, 429]
        finally:
            for cid in cids:
                await _cleanup(cid)


async def test_history_reads_are_rate_limited(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_history_read_per_conversation", "2/60")
    with TestClient(app) as client:
        data = client.post("/chat/session", json={}).json()
        cid, token = data["conversationId"], data["conversationToken"]
        try:
            headers = {CONVERSATION_TOKEN_HEADER: token}
            params = {"history": "true", "conversation_id": cid}
            assert client.get("/chat", params=params, headers=headers).status_code == 200
            assert client.get("/chat", params=params, headers=headers).status_code == 200
            assert client.get("/chat", params=params, headers=headers).status_code == 429
        finally:
            await _cleanup(cid)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

async def test_turn_lock_is_exclusive_across_connections_and_released_on_success_and_error():
    cid = f"lock-{uuid.uuid4().hex[:8]}"
    async with conversation_turn_lock(cid):
        with pytest.raises(TurnInProgress):
            async with conversation_turn_lock(cid):
                pass
        # Another conversation is unaffected.
        async with conversation_turn_lock(cid + "-other"):
            pass
    # Released on success:
    async with conversation_turn_lock(cid):
        pass
    # Released on exception:
    with pytest.raises(RuntimeError):
        async with conversation_turn_lock(cid):
            raise RuntimeError("boom")
    async with conversation_turn_lock(cid):
        pass


async def test_second_simultaneous_turn_for_the_same_conversation_is_refused(monkeypatch, _fake_model):
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_call_ai(session, history, conversation_id, email, name, shop, gate=None):
        started.set()
        await release.wait()
        return {"replyText": "done", "sseEvents": [], "updatedMessages": history}

    monkeypatch.setattr(chat_module, "call_ai", _slow_call_ai)
    with TestClient(app) as client:
        data = client.post("/chat/session", json={}).json()
        cid, token = data["conversationId"], data["conversationToken"]
        try:
            headers = {CONVERSATION_TOKEN_HEADER: token}
            loop = asyncio.get_running_loop()
            first = loop.run_in_executor(None, lambda: client.post("/chat", json={"conversation_id": cid, "message": "one"}, headers=headers))
            await asyncio.wait_for(started.wait(), timeout=10)
            second = await loop.run_in_executor(None, lambda: client.post("/chat", json={"conversation_id": cid, "message": "two"}, headers=headers))
            assert second.status_code == 409 and "retry-after" in second.headers
            release.set()
            assert (await first).status_code == 200
            # Lock released: a later turn works.
            assert client.post("/chat", json={"conversation_id": cid, "message": "three"}, headers=headers).status_code == 200
        finally:
            release.set()
            await _cleanup(cid)


async def test_process_wide_turn_cap_refuses_a_burst_and_recovers(monkeypatch, _fake_model):
    monkeypatch.setattr(settings, "chat_max_concurrent_turns", 1)
    chat_module.turn_slots.active = 0
    with TestClient(app) as client:
        a, b = client.post("/chat/session", json={}).json(), client.post("/chat/session", json={}).json()
        try:
            chat_module.turn_slots.active = 1  # one turn already running elsewhere in this process
            busy = client.post("/chat", json={"conversation_id": a["conversationId"], "message": "hi"}, headers={CONVERSATION_TOKEN_HEADER: a["conversationToken"]})
            assert busy.status_code == 503 and "retry-after" in busy.headers
            chat_module.turn_slots.active = 0
            ok = client.post("/chat", json={"conversation_id": b["conversationId"], "message": "hi"}, headers={CONVERSATION_TOKEN_HEADER: b["conversationToken"]})
            assert ok.status_code == 200
            assert chat_module.turn_slots.active == 0  # released after the stream completed
        finally:
            chat_module.turn_slots.active = 0
            await _cleanup(a["conversationId"])
            await _cleanup(b["conversationId"])
