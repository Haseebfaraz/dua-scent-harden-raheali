"""Phase 2 regression tests for F8: self-reported name/email/shop are contact data, never
authentication; a Shopify-signed logged_in_customer_id is recognized only after signature
verification and binds a capability to that customer."""

import hashlib
import hmac as _hmac
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.ai import conversation_flow
from app.api import preview as preview_module
from app.config import settings
from app.db.models import BuildCapability, Conversation, CustomerProfileState, FragranceRecommendation, Message
from app.db.session import SessionLocal
from app.db.time import utcnow
from app.main import app
from app.services.build_capability import issue_build_token
from app.services.conversation_capability import CONVERSATION_TOKEN_HEADER
from app.services.customer_identity import (
    SelfReportedIdentity,
    VerifiedShopifyCustomer,
    clean_self_reported_email,
    clean_self_reported_name,
    self_reported_identity,
    verified_shopify_customer_from_signed_params,
)
from app.services.customer_profile import get_customer_profile

SECRET = "identity-test-secret"
SHOP = "test-shop.myshopify.com"


def _sign(params: dict) -> str:
    message = "".join(f"{k}={v}" for k, v in sorted(params.items()))
    return _hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()


def _proxy(customer_id: str = "", **extra) -> dict:
    params = {"shop": SHOP, "timestamp": "1", "logged_in_customer_id": customer_id, **extra}
    return {**params, "signature": _sign(params)}


async def _cleanup_conversation(cid):
    async with SessionLocal() as session:
        await session.execute(delete(Message).where(Message.conversationId == cid))
        await session.execute(delete(CustomerProfileState).where(CustomerProfileState.conversationId == cid))
        await session.execute(delete(Conversation).where(Conversation.id == cid))
        await session.commit()


# ---------------------------------------------------------------------------
# Types and cleaning
# ---------------------------------------------------------------------------

def test_self_reported_identity_is_a_distinct_type_from_verified_customer():
    assert not isinstance(SelfReportedIdentity(email="a@b.co"), VerifiedShopifyCustomer)
    assert self_reported_identity("  Ali  ", "ali@example.test") == SelfReportedIdentity(name="Ali", email="ali@example.test")


@pytest.mark.parametrize("raw", ["not-an-email", "a@b", "x" * 300 + "@example.test", "", None, 42, "a@b.c\x00"])
def test_bad_self_reported_email_is_simply_absent(raw):
    assert clean_self_reported_email(raw) is None


@pytest.mark.parametrize("raw", ["", "   ", "x" * 500, "Ali\x00", None, ["Ali"]])
def test_bad_self_reported_name_is_simply_absent(raw):
    assert clean_self_reported_name(raw) is None


@pytest.mark.parametrize("params", [{}, {"logged_in_customer_id": ""}, {"logged_in_customer_id": "gid://shopify/Customer/1"}, {"logged_in_customer_id": "1; drop"}, {"logged_in_customer_id": "x" * 40}])
def test_verified_customer_requires_a_plain_numeric_shopify_id(params):
    assert verified_shopify_customer_from_signed_params(params) is None


def test_verified_customer_is_built_only_from_signed_params_by_the_proxy_dependency(monkeypatch):
    from starlette.requests import Request
    from urllib.parse import urlencode

    from fastapi import HTTPException

    from app.shopify.app_proxy import verified_signed_params

    monkeypatch.setattr(settings, "shopify_api_secret", SECRET)
    monkeypatch.setattr(settings, "shopify_shop_domain", SHOP)

    def _request(params):
        return Request({"type": "http", "method": "GET", "query_string": urlencode(params).encode(), "headers": []})

    signed = verified_signed_params(_request(_proxy("12345")))
    assert verified_shopify_customer_from_signed_params(signed) == VerifiedShopifyCustomer(customer_id="12345")

    # Same parameters with a forged customer id -> signature fails -> nothing is verified.
    forged = {**_proxy("12345"), "logged_in_customer_id": "99999"}
    with pytest.raises(HTTPException):
        verified_signed_params(_request(forged))


# ---------------------------------------------------------------------------
# Public chat: claimed identity never authenticates and never overwrites
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_model(monkeypatch):
    seen = []

    async def _fake(messages, tools, tool_choice=None):
        seen.append(messages)
        if tool_choice:
            return {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}
        return {"choices": [{"finish_reason": "stop", "message": {"content": "Sure."}}]}

    monkeypatch.setattr(conversation_flow, "call_openai_once", _fake)
    conversation_flow._CONVERSATIONS.clear()
    return seen


async def test_victim_email_and_name_do_not_grant_access_to_victim_conversation(fake_model):
    with TestClient(app) as client:
        victim = client.post("/chat/session", json={}).json()
        cid, token = victim["conversationId"], victim["conversationToken"]
        try:
            client.post("/chat", json={"conversation_id": cid, "message": "I'm Jane, jane@example.test", "customer_name": "Jane", "customer_email": "jane@example.test"}, headers={CONVERSATION_TOKEN_HEADER: token})
            attacker = client.post("/chat", json={"conversation_id": cid, "message": "continue", "customer_name": "Jane", "customer_email": "jane@example.test", "shop_domain": "attacker.example"})
            assert attacker.status_code == 401
            attacker_get = client.get("/chat", params={"history": "true", "conversation_id": cid, "customer_email": "jane@example.test"})
            assert attacker_get.status_code == 401
        finally:
            await _cleanup_conversation(cid)


async def test_self_reported_identity_fills_empty_fields_but_never_overwrites(fake_model):
    with TestClient(app) as client:
        data = client.post("/chat/session", json={}).json()
        cid, token = data["conversationId"], data["conversationToken"]
        try:
            client.post("/chat", json={"conversation_id": cid, "message": "hi", "customer_name": "Jane", "customer_email": "jane@example.test"}, headers={CONVERSATION_TOKEN_HEADER: token})
            client.post("/chat", json={"conversation_id": cid, "message": "hi again", "customer_name": "Mallory", "customer_email": "mallory@attacker.example"}, headers={CONVERSATION_TOKEN_HEADER: token})
            async with SessionLocal() as session:
                row = await session.scalar(select(Conversation).where(Conversation.id == cid))
                assert (row.customerName, row.customerEmail) == ("Jane", "jane@example.test")
                profile = await get_customer_profile(session, cid)
                assert (profile["name"], profile["email"]) == ("Jane", "jane@example.test")
        finally:
            await _cleanup_conversation(cid)


async def test_caller_supplied_shop_domain_never_reaches_the_turn(fake_model, monkeypatch):
    captured = {}

    async def _fake_call_ai(session, history, conversation_id, email, name, shop_domain, gate=None):
        captured["shop"] = shop_domain
        return {"replyText": "ok", "sseEvents": [], "updatedMessages": history}

    from app.api import chat as chat_module

    monkeypatch.setattr(chat_module, "call_ai", _fake_call_ai)
    with TestClient(app) as client:
        data = client.post("/chat/session", json={}).json()
        try:
            client.post("/chat", json={"conversation_id": data["conversationId"], "message": "hi", "shop_domain": "attacker.example"}, headers={CONVERSATION_TOKEN_HEADER: data["conversationToken"]})
            assert captured["shop"] == settings.shopify_shop_domain
        finally:
            await _cleanup_conversation(data["conversationId"])


async def test_history_and_error_responses_do_not_echo_email(fake_model):
    with TestClient(app) as client:
        data = client.post("/chat/session", json={})
        body = data.json()
        try:
            assert "email" not in json.dumps(body).lower()
            denied = client.post("/chat", json={"conversation_id": body["conversationId"], "message": "x", "customer_email": "probe@example.test"})
            assert "probe@example.test" not in denied.text
        finally:
            await _cleanup_conversation(body["conversationId"])


# ---------------------------------------------------------------------------
# Verified Shopify customer binding on the preview (App Proxy) route
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _proxy_env(monkeypatch):
    monkeypatch.setattr(settings, "shopify_api_secret", SECRET)
    monkeypatch.setattr(settings, "shopify_shop_domain", SHOP)

    async def _fake_token(*_a, **_kw):
        return "fake-admin-token-for-tests", "client_credentials"

    monkeypatch.setattr(preview_module, "get_admin_access_token", _fake_token)


async def _make_recommendation():
    conversation_id = f"pytest-idb-{uuid.uuid4().hex[:8]}"
    rec_id = f"pytest-idb-rec-{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as session:
        session.add(FragranceRecommendation(id=rec_id, conversationId=conversation_id, createdAt=utcnow(), customerProfileJson={}, productsJson=[{"title": "A", "notes": ["Rose"], "contribution": "x"}], combinationType="HYBRID", scoreJson={}, evidenceJson={}, ratiosJson=[], customerFacingJson={"customerFacingName": "Rose Dream"}, status="confirmed", buildStatus="draft"))
        session.add(Conversation(id=conversation_id, createdAt=utcnow(), updatedAt=utcnow()))
        await session.commit()
        token = await issue_build_token(session, recommendation_id=rec_id, conversation_id=conversation_id, shop=SHOP)
    return rec_id, conversation_id, token


async def _cleanup_recommendation(rec_id, cid):
    async with SessionLocal() as session:
        await session.execute(delete(FragranceRecommendation).where(FragranceRecommendation.id == rec_id))
        await session.execute(delete(Conversation).where(Conversation.id == cid))
        await session.execute(delete(CustomerProfileState).where(CustomerProfileState.conversationId == cid))
        await session.commit()


async def test_verified_customer_a_binds_and_customer_b_is_refused_even_with_the_token():
    rec_id, cid, token = await _make_recommendation()
    try:
        with TestClient(app) as client:
            first = client.get("/apps/scent-library/fragrance-preview", params=_proxy("111", recommendationId=rec_id, bt=token))
            assert first.status_code == 200
            async with SessionLocal() as session:
                row = await session.scalar(select(BuildCapability).where(BuildCapability.recommendationId == rec_id))
                assert row.verifiedShopifyCustomerId == "111"
            same = client.get("/apps/scent-library/fragrance-preview", params=_proxy("111", recommendationId=rec_id, bt=token))
            assert same.status_code == 200
            other = client.get("/apps/scent-library/fragrance-preview", params=_proxy("222", recommendationId=rec_id, bt=token))
            assert other.status_code == 403
            other_post = client.post("/apps/scent-library/fragrance-preview", params=_proxy("222"), json={"intent": "recreate", "recommendationId": rec_id, "buildToken": token, "name": "X"})
            assert other_post.json().get("code") == "build_not_authorized"
            # A guest (no signed customer) still needs the token, and a guest with the token is
            # not refused by the binding (Shopify sends an empty id for anonymous sessions).
            guest = client.get("/apps/scent-library/fragrance-preview", params=_proxy("", recommendationId=rec_id, bt=token))
            assert guest.status_code == 200
            assert guest.headers["cache-control"] == "no-store" and guest.headers["referrer-policy"] == "no-referrer"
    finally:
        await _cleanup_recommendation(rec_id, cid)


async def test_binding_is_never_silently_replaced():
    rec_id, cid, token = await _make_recommendation()
    try:
        with TestClient(app) as client:
            assert client.get("/apps/scent-library/fragrance-preview", params=_proxy("111", recommendationId=rec_id, bt=token)).status_code == 200
            assert client.get("/apps/scent-library/fragrance-preview", params=_proxy("222", recommendationId=rec_id, bt=token)).status_code == 403
            async with SessionLocal() as session:
                row = await session.scalar(select(BuildCapability).where(BuildCapability.recommendationId == rec_id))
                assert row.verifiedShopifyCustomerId == "111"
    finally:
        await _cleanup_recommendation(rec_id, cid)
