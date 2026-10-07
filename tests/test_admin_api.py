"""Merchant dashboard: session-token authentication (no access without a valid token for the
trusted shop) and the read-only admin API (server-side search/pagination, stored snapshots only,
no Shopify or Odoo calls, no secrets or customer payloads in logs). Database-backed."""

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.config import settings
from app.db.models import Conversation, CustomerProfileState, FragranceRecommendation, Message, RecommendationInventoryComponent, RecommendationInventorySnapshot
from app.db.session import SessionLocal
from app.db.time import utcnow
from app.main import app
from app.shopify.session_token import SessionTokenInvalid, verify_session_token

SECRET = "admin-test-secret"
API_KEY = "admin-test-api-key"
SHOP = "test-shop.myshopify.com"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_token(secret=SECRET, alg="HS256", **overrides) -> str:
    now = int(time.time())
    claims = {"iss": f"https://{SHOP}/admin", "dest": f"https://{SHOP}", "aud": API_KEY, "sub": "42", "exp": now + 60, "nbf": now - 5, "iat": now - 5, "jti": "j", "sid": "s"}
    claims.update(overrides)
    head = _b64(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    body = _b64(json.dumps(claims).encode())
    sig = _b64(hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(settings, "shopify_api_secret", SECRET)
    monkeypatch.setattr(settings, "shopify_api_key", API_KEY)
    monkeypatch.setattr(settings, "shopify_shop_domain", SHOP)

    async def _no_external(*_a, **_kw):
        raise AssertionError("the admin dashboard must never call Shopify or Odoo")

    monkeypatch.setattr("app.shopify.admin_client.admin_graphql", _no_external)


def _auth(token=None) -> dict:
    return {"Authorization": f"Bearer {token or make_token()}"}


# ---------------------------------------------------------------- session token

def test_valid_session_token_is_accepted():
    merchant = verify_session_token(make_token())
    assert merchant.shop == SHOP and merchant.user_id == "42"


@pytest.mark.parametrize("token", [
    make_token(secret="wrong-secret"),
    make_token(aud="another-app"),
    make_token(exp=int(time.time()) - 120),
    make_token(nbf=int(time.time()) + 120),
    make_token(dest="https://other-shop.myshopify.com"),
    make_token(iss="https://other-shop.myshopify.com/admin"),
    make_token(alg="none"),
    "not-a-jwt",
    "",
    None,
])
def test_invalid_session_tokens_are_refused(token):
    with pytest.raises(SessionTokenInvalid):
        verify_session_token(token)


def test_session_tokens_fail_closed_without_app_credentials(monkeypatch):
    monkeypatch.setattr(settings, "shopify_api_secret", "")
    with pytest.raises(SessionTokenInvalid):
        verify_session_token(make_token())


@pytest.mark.parametrize("path", ["/admin/api/customers", "/admin/api/customers/x", "/admin/api/customers/x/messages", "/admin/api/customers/x/recommendations", "/admin/api/activity", "/admin/api/readiness"])
def test_every_admin_api_route_requires_a_merchant_session(path):
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
        assert client.get(path, headers=_auth(make_token(secret="wrong"))).status_code == 401


def test_admin_shell_is_only_frameable_by_the_shopify_admin():
    with TestClient(app) as client:
        response = client.get("/admin")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == f"frame-ancestors https://{SHOP} https://admin.shopify.com;"
    assert f'content="{API_KEY}"' in response.text


# ---------------------------------------------------------------- data

async def _seed(email: str, name: str, *, build_status="saved", with_snapshot=True, messages=3) -> tuple[str, str]:
    conversation_id = f"pytest-admin-{uuid.uuid4().hex[:10]}"
    rec_id = f"pytest-admin-rec-{uuid.uuid4().hex[:10]}"
    now = utcnow()
    async with SessionLocal() as session:
        session.add(Conversation(id=conversation_id, customerEmail=email, customerName=name, createdAt=now, updatedAt=now))
        await session.flush()
        session.add(CustomerProfileState(id=uuid.uuid4().hex, conversationId=conversation_id, createdAt=now, updatedAt=now,
                                         profileJson={"name": name, "email": email, "city": "Austin", "country": "United States", "likes": ["Rose"], "occasion": "date night", "pendingRecreateRecommendationId": "internal"}))
        for i in range(messages):
            session.add(Message(id=uuid.uuid4().hex, conversationId=conversation_id, role="user" if i % 2 == 0 else "assistant", content=f"message {i}", createdAt=now + timedelta(seconds=i)))
        session.add(Message(id=uuid.uuid4().hex, conversationId=conversation_id, role="tool", content="internal tool payload", createdAt=now))
        session.add(FragranceRecommendation(
            id=rec_id, conversationId=conversation_id, customerProfileJson={}, productsJson=[{"title": "Rose Oud", "contribution": "anchor", "notes": ["Rose"]}],
            combinationType="HYBRID", scoreJson={"confidence": 0.8, "confidenceBreakdown": {"data": {"level": "high"}}, "riskPenalty": 2, "riskBreakdown": [{"factor": "x"}],
                                                 "matchedExactNotes": ["Rose"], "missingExactNotes": [], "exactNoteCoverageScore": 1},
            evidenceJson={"canonicalKey": "k"}, ratiosJson=[{"productTitle": "Rose Oud", "ratioPercent": 100}], customerFacingJson={"customerFacingName": "Velvet Bloom"},
            status="confirmed", buildStatus=build_status, shopifyProductId="gid://shopify/Product/123" if build_status == "saved" else None, createdAt=now,
        ))
        await session.flush()
        if with_snapshot:
            snap_id = uuid.uuid4().hex
            session.add(RecommendationInventorySnapshot(id=snap_id, recommendationId=rec_id, inventoryValidated=True, buildable=True, checkedAt=now, oilTotalMl=10, alcoholMl=24, requestStatus="ok", createdAt=now))
            await session.flush()
            session.add(RecommendationInventoryComponent(id=uuid.uuid4().hex, snapshotId=snap_id, productTitle="Rose Oud", odooSku="DUA-ROSE_Oil", ratioPercent=100, requiredOilMl=10, onHandQty=500, mappingStatus="CONNECTED", sufficient=True))
        await session.commit()
    return conversation_id, rec_id


async def _cleanup(*conversation_ids):
    async with SessionLocal() as session:
        await session.execute(delete(FragranceRecommendation).where(FragranceRecommendation.conversationId.in_(conversation_ids)))
        await session.execute(delete(CustomerProfileState).where(CustomerProfileState.conversationId.in_(conversation_ids)))
        await session.execute(delete(Message).where(Message.conversationId.in_(conversation_ids)))
        await session.execute(delete(Conversation).where(Conversation.id.in_(conversation_ids)))
        await session.commit()


async def test_customer_search_is_server_side_and_paginated():
    tag = uuid.uuid4().hex[:8]
    ids = [(await _seed(f"buyer{i}-{tag}@example.com", f"Customer {tag} {i}"))[0] for i in range(3)]
    other, _ = await _seed(f"someone-else-{uuid.uuid4().hex[:6]}@example.com", "Other Person")
    try:
        with TestClient(app) as client:
            page1 = client.get("/admin/api/customers", params={"q": tag, "pageSize": 2}, headers=_auth()).json()
            page2 = client.get("/admin/api/customers", params={"q": tag, "pageSize": 2, "page": 2}, headers=_auth()).json()
            by_name = client.get("/admin/api/customers", params={"q": f"Customer {tag} 1"}, headers=_auth()).json()
            wildcard = client.get("/admin/api/customers", params={"q": "%"}, headers=_auth()).json()
        assert page1["total"] == 3 and len(page1["customers"]) == 2 and len(page2["customers"]) == 1
        assert {c["conversationId"] for c in page1["customers"] + page2["customers"]} == set(ids)
        assert [c["email"] for c in by_name["customers"]] == [f"buyer1-{tag}@example.com"]
        row = page1["customers"][0]
        assert row["location"] == "Austin, United States" and row["likes"] == ["Rose"] and row["occasion"] == "date night"
        assert row["recommendationCount"] == 1 and row["buildCount"] == 1
        assert all("%" not in (c["email"] or "") for c in wildcard["customers"])  # "%" is literal, not a wildcard
    finally:
        await _cleanup(*ids, other)


async def test_customer_detail_messages_and_recommendations_use_stored_data_only():
    conversation_id, rec_id = await _seed("detail@example.com", "Dana", build_status="pending_review", messages=5)
    try:
        with TestClient(app) as client:
            detail = client.get(f"/admin/api/customers/{conversation_id}", headers=_auth()).json()
            msgs = client.get(f"/admin/api/customers/{conversation_id}/messages", params={"pageSize": 2}, headers=_auth()).json()
            recs = client.get(f"/admin/api/customers/{conversation_id}/recommendations", headers=_auth()).json()
            missing = client.get("/admin/api/customers/does-not-exist", headers=_auth())
        assert detail["email"] == "detail@example.com" and detail["messageCount"] == 5
        assert "pendingRecreateRecommendationId" not in detail["profile"]
        assert msgs["total"] == 5 and [m["content"] for m in msgs["messages"]] == ["message 3", "message 4"]  # newest page, oldest first
        assert all(m["role"] in ("user", "assistant") for m in msgs["messages"])
        rec = recs["recommendations"][0]
        assert rec["id"] == rec_id and rec["needsReview"] is True and rec["buildStatus"] == "pending_review"
        assert rec["products"][0]["title"] == "Rose Oud"  # internal components: merchant view only
        assert rec["exactNotes"]["matched"] == ["Rose"] and rec["confidenceBreakdown"] == {"data": {"level": "high"}}
        assert rec["inventory"]["components"][0]["odooSku"] == "DUA-ROSE_Oil" and rec["inventory"]["requestStatus"] == "ok"
        assert missing.status_code == 404
    finally:
        await _cleanup(conversation_id)


async def test_activity_lists_builds_needing_review_without_retrying_them():
    conversation_id, rec_id = await _seed("activity@example.com", "Ari", build_status="creating", with_snapshot=False)
    try:
        with TestClient(app) as client:
            data = client.get("/admin/api/activity", params={"attentionOnly": "true", "pageSize": 50}, headers=_auth()).json()
        assert data["summary"]["needsReview"] >= 1
        item = next(i for i in data["items"] if i["id"] == rec_id)
        assert item["needsReview"] is True and item["inventory"] is None
        assert all(i["needsReview"] for i in data["items"])
        async with SessionLocal() as session:
            assert (await session.get(FragranceRecommendation, rec_id)).buildStatus == "creating"  # untouched
    finally:
        await _cleanup(conversation_id)


async def test_admin_logs_carry_no_token_or_customer_data(caplog):
    conversation_id, _ = await _seed("private@example.com", "Priya Private")
    token = make_token()
    try:
        with caplog.at_level(logging.INFO):
            with TestClient(app) as client:
                client.get("/admin/api/customers", params={"q": "private@example.com"}, headers=_auth(token))
                client.get(f"/admin/api/customers/{conversation_id}/messages", headers=_auth(token))
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "ADMIN_ACCESS" in logged
        for secret in (token, "private@example.com", "Priya", "message 0", SECRET):
            assert secret not in logged
    finally:
        await _cleanup(conversation_id)
