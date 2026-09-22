"""Phase 9 (B15 / F6): every outbound model request is counted at the send boundary and bounded per
customer turn.

The transport (`httpx.AsyncClient.post`) is replaced with an in-process fake in every test here, so
the counts below are counts of requests that would actually have left the process, including the
bounded parameter-correction resends after a 400. Nothing patches `call_openai_once` or
`call_copy_model`: the budget code under test sits in front of the real request functions.
"""

import asyncio
import contextvars
import json
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.ai import conversation_flow, model_budget, tool_executor
from app.ai.conversation_flow import call_ai
from app.ai.model_budget import begin_turn_budget, current_budget
from app.ai.security_gate import GateDecision
from app.config import settings
from app.db.models import Conversation, ConversationCapability, CustomerProfileState, FragranceRecommendation, Message, MessageSecurityClassification
from app.db.session import SessionLocal
from app.main import app
from app.services import copy_generation
from app.services.conversation_capability import CONVERSATION_TOKEN_HEADER
from app.services.customer_profile import save_customer_profile_fields
from app.services.recommendation_engine import DEFAULT_MAX_RESULTS
from tests.security.model_boundary import CANARIES, FORBIDDEN_MODEL_KEYS, _KEY_PATTERN
from tests.synthetic_catalog import EXISTING_HYBRID, PRODUCTS

SHOP = "test-shop.myshopify.com"
SOURCE_TITLES = [p["title"] for p in PRODUCTS] + [EXISTING_HYBRID["title"]]


class FakeTransport:
    """Replaces httpx.AsyncClient.post. Records every request that would have been sent."""

    def __init__(self):
        self.requests: list[dict] = []
        self.completion_reply = "Here is a warm note on what you said."
        self.completion_tool_calls: list[dict] | None = None   # returned by EVERY non-forced completion when set
        self.copy_text = None                                   # None = distinct text per call
        self.status_plan: dict[str, list] = {}                  # kind -> queue of status codes / correction labels
        self.hang = False                                       # block forever (cancellation test)
        self.cancelled = 0
        self.gate = asyncio.Event()

    def _kind(self, payload: dict) -> str:
        if payload.get("response_format"):
            return "copy"
        forced = payload.get("tool_choice")
        if isinstance(forced, dict):
            return "classifier" if forced["function"]["name"] == "classify_customer_message" else "extraction"
        return "completion"

    async def post(self, url, json=None, headers=None, **_kw):
        kind = self._kind(json)
        self.requests.append({"kind": kind, "payload": json})
        request = httpx.Request("POST", url)
        if self.hang:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        queue = self.status_plan.get(kind) or []
        if queue:
            status = queue.pop(0)
            if status == "400-temperature":
                return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'temperature' does not support this model"}}, request=request)
            if status == "400-max_tokens":
                return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'max_tokens' is not supported, use 'max_completion_tokens' instead"}}, request=request)
            if status != 200:
                return httpx.Response(status, json={"error": {"message": "x"}}, request=request)
        n = len(self.requests)
        if kind == "copy":
            text = self.copy_text or f"{_WORDS[n % len(_WORDS)]} opening number {n} with a soft base"
            return httpx.Response(200, json={"choices": [{"message": {"content": json_dumps({"description": text, "whySuits": f"{text} because you asked."})}}]}, request=request)
        if kind == "extraction":
            return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
                {"id": "x", "type": "function", "function": {"name": "record_profile_updates", "arguments": "{\"fieldsToUpdate\": []}"}}]}}]}, request=request)
        if kind == "classifier":
            return httpx.Response(500, json={}, request=request)
        if self.completion_tool_calls:
            return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": self.completion_tool_calls}}]}, request=request)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": self.completion_reply}}]}, request=request)

    def count(self, kind: str | None = None) -> int:
        return len([r for r in self.requests if kind is None or r["kind"] == kind])


_WORDS = ["Bright", "Warm", "Soft", "Crisp", "Airy", "Deep", "Smooth", "Fresh", "Golden", "Quiet", "Sunny", "Velvet", "Cool", "Gentle", "Radiant", "Calm", "Vivid", "Mellow", "Sharp", "Tender",
          "Lush", "Clean", "Dusky", "Silky", "Bold", "Sheer", "Rich", "Light", "Misty", "Zesty", "Woody", "Sweet", "Green", "Salty", "Balmy", "Hazy", "Plush", "Wild", "Ripe", "Glowing",
          "Breezy", "Earthy", "Creamy", "Powdery", "Smoky", "Juicy", "Frosty", "Honeyed", "Peppery", "Dewy"]


def json_dumps(value) -> str:
    return json.dumps(value)


@pytest.fixture
def transport(monkeypatch):
    fake = FakeTransport()

    async def _post(_client, url, **kw):
        return await fake.post(url, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    monkeypatch.setattr(settings, "openai_api_key", "fake-key-for-tests")
    monkeypatch.setattr(settings, "shopify_shop_domain", SHOP)
    conversation_flow._CONVERSATIONS.clear()
    begin_turn_budget()
    return fake


def _items(n: int) -> list[dict]:
    return [{"proposal": {"customerFacingDescription": f"template character {i}", "customerFacingWhySuits": f"template reason {i}", "confidence": "medium", "evidenceScope": "limited"},
             "notesByRole": {"top": ["Lemon"], "base": ["Musk"]}} for i in range(n)]


def _conv() -> str:
    return f"pytest-cost9-{uuid.uuid4().hex[:10]}"


async def _cleanup(conversation_id: str) -> None:
    async with SessionLocal() as session:
        for rid in list(await session.scalars(select(FragranceRecommendation.id).where(FragranceRecommendation.conversationId == conversation_id))):
            await session.execute(delete(FragranceRecommendation).where(FragranceRecommendation.id == rid))
        ids = list(await session.scalars(select(Message.id).where(Message.conversationId == conversation_id)))
        await session.execute(delete(MessageSecurityClassification).where(MessageSecurityClassification.messageId.in_(ids or ["-"])))
        await session.execute(delete(Message).where(Message.conversationId == conversation_id))
        await session.execute(delete(CustomerProfileState).where(CustomerProfileState.conversationId == conversation_id))
        await session.execute(delete(ConversationCapability).where(ConversationCapability.conversationId == conversation_id))
        await session.execute(delete(Conversation).where(Conversation.id == conversation_id))
        await session.commit()


# ===========================================================================
# 1. The bounds, as configured, and the arithmetic behind the default
# ===========================================================================

def test_every_bound_is_server_configured_and_not_model_reachable():
    assert settings.chat_max_model_requests_per_turn == 48
    assert settings.chat_max_tool_turns == 10 and settings.chat_max_tool_calls_per_turn == 6
    assert settings.openai_max_output_tokens == 700 and settings.openai_copy_max_output_tokens == 200
    assert settings.openai_timeout_seconds == 30 and copy_generation._COPY_REQUEST_TIMEOUT_SECONDS == 12.0
    assert settings.chat_turn_deadline_seconds == 90 and settings.security_gate_classifier_timeout_seconds == 8.0
    # copy: one call per proposal plus at most one retry wave; each call at most 3 sends (two corrections)
    assert DEFAULT_MAX_RESULTS == 8
    assert "maximumResults" not in json.dumps([t for t in __import__("app.ai.tools", fromlist=["x"]).FRAGRANCE_AGENT_TOOLS])  # not a model argument
    # a full generation turn fits the default budget with headroom
    assert 1 + 1 + 3 + 1 + DEFAULT_MAX_RESULTS * 2 <= settings.chat_max_model_requests_per_turn


# ===========================================================================
# 2. Copy generation: exact counts, retries, corrections, exhaustion, fallback
# ===========================================================================

async def test_copy_calls_are_one_per_proposal_when_nothing_collides(transport):
    items = _items(8)
    await copy_generation.apply_customer_facing_copy(items, {"likes": ["fresh"]}, [])
    assert transport.count("copy") == 8
    assert all(i["proposal"]["customerFacingDescription"].split()[1:3] == ["opening", "number"] for i in items)


async def test_copy_retry_wave_is_at_most_one_per_proposal(transport):
    transport.copy_text = "Bright citrus over musk"  # identical text: every later item collides
    items = _items(8)
    await copy_generation.apply_customer_facing_copy(items, {"likes": ["fresh"]}, [])
    assert transport.count("copy") == 8 + 7  # wave 1 for all, one retry for the 7 collisions; never a third wave
    assert sum(1 for i in items if i["proposal"]["customerFacingDescription"] == "Bright citrus over musk") == 1
    assert sum(1 for i in items if i["proposal"]["customerFacingDescription"].startswith("template character")) == 7  # deterministic fallback kept


async def test_copy_parameter_corrections_are_bounded_to_two_extra_sends(transport):
    transport.status_plan["copy"] = ["400-temperature", "400-max_tokens", "400-temperature", "400-temperature"]  # the provider keeps rejecting
    items = _items(1)
    await copy_generation.apply_customer_facing_copy(items, {}, [])
    assert transport.count("copy") == 3  # first send + the two distinct corrections, then give up (a hard failure gets no retry wave)
    assert items[0]["proposal"]["customerFacingDescription"] == "template character 0"


async def test_copy_stops_exactly_at_the_budget_and_keeps_the_template_text(transport):
    budget = begin_turn_budget(limit=5)
    items = _items(8)
    await copy_generation.apply_customer_facing_copy(items, {"likes": ["fresh"]}, [])
    assert transport.count("copy") == 5 and budget.started == 5 and budget.refused >= 3
    assert sum(1 for i in items if i["proposal"]["customerFacingDescription"].split()[1:3] == ["opening", "number"]) == 5
    assert sum(1 for i in items if i["proposal"]["customerFacingDescription"].startswith("template character")) == 3


async def test_large_candidate_set_is_still_capped_by_the_turn_budget(transport):
    transport.copy_text = "Same text every time"  # forces a retry for every item after the first
    items = _items(40)  # far more than the engine ever returns
    await copy_generation.apply_customer_facing_copy(items, {}, [])
    assert transport.count("copy") == settings.chat_max_model_requests_per_turn  # 48, not 79
    assert current_budget().refused > 0


# ===========================================================================
# 3. The conversation loop: repeated tool requests, repair, exhaustion, classifier
# ===========================================================================

async def _history(text: str) -> list[dict]:
    return [{"role": "assistant", "content": "What are you in the mood for?"}, {"role": "user", "content": text}]


async def test_repeated_model_tool_requests_are_bounded_by_the_tool_turn_limit(transport, db_session):
    cid = _conv()
    try:
        transport.completion_tool_calls = [{"id": "t", "type": "function", "function": {"name": "save_customer_profile_field", "arguments": json.dumps({"field": "additionalPreferences", "value": "something new"})}}]
        result = await call_ai(db_session, await _history("Build me a custom fragrance for a wedding."), cid, None, None, SHOP, gate=GateDecision("FRAGRANCE", "NONE", None))
        assert transport.count("extraction") == 1 and transport.count("completion") == settings.chat_max_tool_turns
        assert transport.count() == 1 + settings.chat_max_tool_turns and transport.count("copy") == 0
        assert result["replyText"]  # the customer still gets a reply
    finally:
        await _cleanup(cid)


async def test_exhausted_budget_stops_the_loop_with_the_outage_reply_and_no_further_sends(transport, db_session):
    cid = _conv()
    try:
        budget = begin_turn_budget(limit=3)
        transport.completion_tool_calls = [{"id": "t", "type": "function", "function": {"name": "save_customer_profile_field", "arguments": json.dumps({"field": "additionalPreferences", "value": "again"})}}]
        result = await call_ai(db_session, await _history("Build me a custom fragrance for a wedding."), cid, None, None, SHOP, gate=GateDecision("FRAGRANCE", "NONE", None))
        assert transport.count() == 3 and budget.started == 3 and budget.refused == 1
        assert "trouble reaching" in result["replyText"]
        assert "preview_ready" not in json.dumps(result["sseEvents"])
    finally:
        await _cleanup(cid)


async def test_privacy_repair_is_exactly_one_extra_send(transport, db_session):
    cid = _conv()
    try:
        transport.completion_reply = "Sure. It is built the way Dua does it."  # the brand name: a privacy violation, repaired once
        result = await call_ai(db_session, await _history("Build me a custom fragrance for a wedding."), cid, None, None, SHOP, gate=GateDecision("FRAGRANCE", "NONE", None))
        assert transport.count("completion") == 2 and transport.count("extraction") == 1  # main + one repair
        assert result["replyText"]
    finally:
        await _cleanup(cid)


async def test_classifier_attempt_is_counted_in_the_same_turn_budget(transport):
    with TestClient(app) as client:
        boot = client.post("/chat/session", json={}).json()
        cid, token = boot["conversationId"], boot["conversationToken"]
        try:
            response = client.post("/chat", json={"conversation_id": cid, "message": "Can you recreate it so I can adjust the balance?"}, headers={CONVERSATION_TOKEN_HEADER: token})
            assert response.status_code == 200
            assert transport.count() == 1 and transport.requests[0]["kind"] == "classifier"  # unresolved: nothing else sent
        finally:
            await _cleanup(cid)


# ===========================================================================
# 4. Deadline cancels outstanding work; budgets are per turn, not shared
# ===========================================================================

async def test_turn_deadline_cancels_the_hanging_request_and_nothing_starts_afterwards(transport, monkeypatch):
    monkeypatch.setattr(settings, "chat_turn_deadline_seconds", 1)
    transport.hang = True
    with TestClient(app) as client:
        boot = client.post("/chat/session", json={}).json()
        cid, token = boot["conversationId"], boot["conversationToken"]
        try:
            started = time.monotonic()
            response = client.post("/chat", json={"conversation_id": cid, "message": "Build me a custom fragrance for a wedding, something fresh."}, headers={CONVERSATION_TOKEN_HEADER: token})
            elapsed = time.monotonic() - started
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            assert any(e.get("type") == "error" and "too long" in e.get("error", "") for e in events), events
            assert elapsed < 5
            sent_at_deadline = transport.count()
            await asyncio.sleep(0.5)
            assert transport.count() == sent_at_deadline == 1  # the extraction request hung; nothing else was ever sent
            assert transport.cancelled == 1  # the outstanding request was cancelled, not left running
        finally:
            transport.gate.set()
            await _cleanup(cid)


async def test_budgets_are_isolated_between_simultaneous_conversations(transport):
    transport.copy_text = "Same text"  # 8 + 7 sends per conversation

    async def one_turn() -> int:
        budget = begin_turn_budget(limit=20)
        await copy_generation.apply_customer_facing_copy(_items(8), {}, [])
        return budget.started

    results = await asyncio.gather(asyncio.create_task(one_turn()), asyncio.create_task(one_turn()))
    assert results == [15, 15] and transport.count("copy") == 30  # neither turn saw the other's spending


def test_budget_is_bound_to_the_context_and_defaults_to_the_setting():
    outer = begin_turn_budget()
    assert outer.limit == settings.chat_max_model_requests_per_turn and outer.remaining == outer.limit

    def inner():
        return begin_turn_budget(limit=2) is not outer and current_budget().limit == 2

    assert contextvars.copy_context().run(inner)
    assert current_budget() is outer  # the child context's budget did not leak back
    assert model_budget.try_start("completion") and outer.started == 1


# ===========================================================================
# 5. Positive control: the real pipeline still yields a safe recommendation with copy exhausted
# ===========================================================================

@pytest.mark.usefixtures("synthetic_catalog")
async def test_generation_with_no_copy_budget_still_produces_a_safe_recommendation(transport, db_session):
    cid = _conv()
    try:
        await save_customer_profile_fields(db_session, cid, {
            "likes": ["fresh", "citrus"], "dislikes": ["oud"], "occasion": "wedding", "strengthPreference": "moderate",
            "city": "Los Angeles", "country": "United States", "locationVerified": True, "locationSource": "order_history", "name": "Sam", "email": "sam@example.com",
        })
        ctx = {"conversationId": cid, "customerName": "Sam", "customerEmail": "sam@example.com", "shopDomain": SHOP}
        begin_turn_budget(limit=0)  # every copy send refused
        outcome = await tool_executor.run_generate(db_session, cid, ctx)
        assert outcome["ok"] and outcome["sseEvent"]["type"] == "preview_ready"
        assert transport.count() == 0
        blob = json.dumps(outcome["modelContent"])
        for title in SOURCE_TITLES:
            assert title not in blob
        for canary in CANARIES.values():
            assert canary not in blob
        assert not (set(_KEY_PATTERN.findall(outcome["modelContent"])) & FORBIDDEN_MODEL_KEYS)
        assert '"whySuits": "' in outcome["modelContent"] and '"whySuits": null' not in outcome["modelContent"]  # template copy present

        begin_turn_budget()
        tool_executor._conversation_scratch.pop(cid, None)
        await save_customer_profile_fields(db_session, cid, {"selectedRecommendationId": None, "additionalPreferences": "airy"})
        outcome = await tool_executor.run_generate(db_session, cid, ctx)
        assert outcome["ok"] and transport.count("copy") == DEFAULT_MAX_RESULTS  # with budget: exactly one send per proposal
    finally:
        await _cleanup(cid)
