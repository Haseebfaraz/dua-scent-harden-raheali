"""Phase 1 regression tests for N3 / F2 on the App Proxy preview route: a signed proxy request
plus a recommendation id is NOT enough to read or mutate a build; the build capability minted
for that exact recommendation is required, and no draft/profile/Shopify write happens without it.
Database-backed (schema-only local Postgres is enough)."""

import hashlib
import hmac as _hmac
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.api import preview as preview_module
from app.config import settings
from app.db.models import Conversation, CustomerProfileState, FragranceRecommendation
from app.db.session import SessionLocal
from app.db.time import utcnow
from app.main import app
from app.services.build_capability import issue_build_token
from app.services.customer_profile import get_customer_profile

SECRET = "preview-auth-test-secret"
SHOP = "test-shop.myshopify.com"


def _sign(params: dict) -> str:
    message = "".join(f"{k}={v}" for k, v in sorted(params.items()))
    return _hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()


def _proxy(**extra) -> dict:
    params = {"shop": SHOP, "timestamp": "1", "logged_in_customer_id": "", **extra}
    return {**params, "signature": _sign(params)}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(settings, "shopify_api_secret", SECRET)
    monkeypatch.setattr(settings, "shopify_shop_domain", SHOP)

    async def _fake_token(*_a, **_kw):
        return "fake-admin-token-for-tests", "client_credentials"

    monkeypatch.setattr(preview_module, "get_admin_access_token", _fake_token)


@pytest.fixture
def shopify_writes(monkeypatch):
    calls = []

    async def _create(*a, **kw):
        calls.append("create")
        return {"productId": "gid://shopify/Product/1", "variantId": "gid://shopify/ProductVariant/1", "price": 60.0, "productUrl": f"https://{SHOP}/products/x"}

    async def _reprice(*a, **kw):
        calls.append("reprice")
        return {"price": "60.00", "variantId": "gid://shopify/ProductVariant/2", "created": True}

    async def _handle(*a, **kw):
        return "x"

    monkeypatch.setattr(preview_module, "create_shopify_build_product", _create)
    monkeypatch.setattr(preview_module, "reprice_existing_build", _reprice)
    monkeypatch.setattr(preview_module, "get_product_handle", _handle)
    return calls


async def _make_recommendation(**overrides):
    conversation_id = f"pytest-pa-{uuid.uuid4().hex[:8]}"
    defaults = dict(
        id=f"pytest-pa-rec-{uuid.uuid4().hex[:8]}", conversationId=conversation_id, customerProfileJson={"likes": ["Rose"]},
        productsJson=[{"title": "Rose Oud", "notes": ["Rose", "Oud"], "contribution": "anchor"}], combinationType="HYBRID",
        scoreJson={}, evidenceJson={}, ratiosJson=[{"productTitle": "Rose Oud", "ratioPercent": 100}],
        customerFacingJson={"customerFacingName": "Rose Dream"}, status="confirmed", buildStatus="draft",
    )
    defaults.update(overrides)
    async with SessionLocal() as session:
        session.add(FragranceRecommendation(createdAt=utcnow(), **defaults))
        session.add(Conversation(id=conversation_id, createdAt=utcnow(), updatedAt=utcnow()))
        await session.commit()
        token = await issue_build_token(session, recommendation_id=defaults["id"], conversation_id=conversation_id, shop=SHOP)
    return defaults["id"], conversation_id, token


async def _cleanup(recommendation_id, conversation_id):
    async with SessionLocal() as session:
        await session.execute(delete(FragranceRecommendation).where(FragranceRecommendation.id == recommendation_id))
        await session.execute(delete(Conversation).where(Conversation.id == conversation_id))
        await session.execute(delete(CustomerProfileState).where(CustomerProfileState.conversationId == conversation_id))
        await session.commit()


async def _draft_state(recommendation_id):
    async with SessionLocal() as session:
        rec = await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == recommendation_id))
        return rec.draftName, rec.draftRatiosJson, rec.buildStatus, rec.shopifyProductId


RATIOS = {"top": 40, "middle": 30, "base": 30}


async def test_get_without_token_is_refused_and_reveals_nothing():
    rec_id, conv_id, _token = await _make_recommendation()
    try:
        with TestClient(app) as client:
            response = client.get("/apps/scent-library/fragrance-preview", params=_proxy(recommendationId=rec_id))
        assert response.status_code == 403
        assert "Rose Dream" not in response.text and rec_id not in response.text
    finally:
        await _cleanup(rec_id, conv_id)


async def test_get_with_another_recommendations_token_is_refused():
    rec_a, conv_a, token_a = await _make_recommendation()
    rec_b, conv_b, _token_b = await _make_recommendation()
    try:
        with TestClient(app) as client:
            response = client.get("/apps/scent-library/fragrance-preview", params=_proxy(recommendationId=rec_b, bt=token_a))
        assert response.status_code == 403
    finally:
        await _cleanup(rec_a, conv_a)
        await _cleanup(rec_b, conv_b)


async def test_get_with_the_right_token_renders_and_embeds_the_capability():
    rec_id, conv_id, token = await _make_recommendation()
    try:
        with TestClient(app) as client:
            response = client.get("/apps/scent-library/fragrance-preview", params=_proxy(recommendationId=rec_id, bt=token))
        assert response.status_code == 200
        assert "Rose Dream" in response.text
        assert f'"buildToken": "{token}"' in response.text
    finally:
        await _cleanup(rec_id, conv_id)


async def test_signed_request_for_a_different_shop_is_refused_even_with_a_valid_token():
    rec_id, conv_id, token = await _make_recommendation()
    try:
        params = {"shop": "other-store.myshopify.com", "timestamp": "1", "recommendationId": rec_id, "bt": token}
        with TestClient(app) as client:
            response = client.get("/apps/scent-library/fragrance-preview", params={**params, "signature": _sign(params)})
        assert response.status_code == 403
    finally:
        await _cleanup(rec_id, conv_id)


@pytest.mark.parametrize("intent", ["recreate", "save_build", "add_to_cart"])
async def test_mutation_intents_without_a_valid_token_write_nothing(shopify_writes, intent):
    rec_id, conv_id, token = await _make_recommendation()
    other_id, other_conv, other_token = await _make_recommendation()
    try:
        with TestClient(app) as client:
            for body in [
                {"intent": intent, "recommendationId": rec_id, "name": "Hijacked", "ratios": RATIOS},                            # id alone
                {"intent": intent, "recommendationId": rec_id, "buildToken": "wrong", "name": "Hijacked", "ratios": RATIOS},
                {"intent": intent, "recommendationId": rec_id, "buildToken": other_token, "name": "Hijacked", "ratios": RATIOS},  # token for B on A
            ]:
                response = client.post("/apps/scent-library/fragrance-preview", params=_proxy(), json=body)
                assert response.status_code == 200
                assert response.json().get("code") == "build_not_authorized"
        assert shopify_writes == []
        assert await _draft_state(rec_id) == (None, None, "draft", None)
        async with SessionLocal() as session:
            assert (await get_customer_profile(session, conv_id))["pendingRecreateRecommendationId"] is None
    finally:
        await _cleanup(rec_id, conv_id)
        await _cleanup(other_id, other_conv)


@pytest.mark.parametrize("ratios", [{"top": 1, "middle": 1, "base": 1}, {"top": -100, "middle": 100, "base": 100}, {"top": 50, "middle": 50}, None])
async def test_invalid_ratios_on_save_build_write_nothing(shopify_writes, ratios):
    rec_id, conv_id, token = await _make_recommendation()
    try:
        with TestClient(app) as client:
            response = client.post("/apps/scent-library/fragrance-preview", params=_proxy(), json={"intent": "save_build", "recommendationId": rec_id, "buildToken": token, "ratios": ratios})
        assert response.json().get("code") == "invalid_input"
        assert shopify_writes == []
        assert await _draft_state(rec_id) == (None, None, "draft", None)
    finally:
        await _cleanup(rec_id, conv_id)


async def test_authorized_save_build_creates_the_product(shopify_writes):
    rec_id, conv_id, token = await _make_recommendation()
    try:
        with TestClient(app) as client:
            response = client.post("/apps/scent-library/fragrance-preview", params=_proxy(), json={"intent": "save_build", "recommendationId": rec_id, "buildToken": token, "name": "Rose Dream", "ratios": RATIOS})
        assert response.status_code == 200
        assert response.json()["status"] == "saved"
        assert shopify_writes == ["create"]
        assert (await _draft_state(rec_id))[2:] == ("saved", "gid://shopify/Product/1")
    finally:
        await _cleanup(rec_id, conv_id)


async def test_authorized_add_to_cart_on_an_existing_product_reprices(shopify_writes):
    rec_id, conv_id, token = await _make_recommendation(shopifyProductId="gid://shopify/Product/9", shopifyVariantId="gid://shopify/ProductVariant/9")
    try:
        with TestClient(app) as client:
            response = client.post("/apps/scent-library/fragrance-preview", params=_proxy(), json={"intent": "add_to_cart", "recommendationId": rec_id, "buildToken": token, "ratios": RATIOS})
        assert response.status_code == 200
        assert response.json()["cartUrl"] == f"https://{SHOP}/cart/2:1"
        assert shopify_writes == ["reprice"]
    finally:
        await _cleanup(rec_id, conv_id)


async def test_authorized_recreate_marks_draft_and_flags_profile(shopify_writes):
    rec_id, conv_id, token = await _make_recommendation()
    try:
        with TestClient(app) as client:
            response = client.post("/apps/scent-library/fragrance-preview", params=_proxy(), json={"intent": "recreate", "recommendationId": rec_id, "buildToken": token, "name": "New Name", "ratios": RATIOS})
        assert response.json() == {"status": "recreate", "redirectUrl": f"https://{SHOP}/"}
        assert (await _draft_state(rec_id))[:2] == ("New Name", RATIOS)
        async with SessionLocal() as session:
            assert (await get_customer_profile(session, conv_id))["pendingRecreateRecommendationId"] == rec_id
        assert shopify_writes == []
    finally:
        await _cleanup(rec_id, conv_id)
