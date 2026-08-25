import hashlib
import hmac as _hmac
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.api import preview as preview_module
from app.config import settings
from app.db.models import Conversation, CustomerProfileState, FragranceRecommendation
from app.db.session import SessionLocal
from app.main import app

SECRET = "preview-test-secret"
SHOP = "test-shop.myshopify.com"


def _sign(params: dict) -> str:
    message = "".join(f"{k}={v}" for k, v in sorted(params.items()))
    return _hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()


def _proxy_params(**extra) -> dict:
    params = {"shop": SHOP, "timestamp": "1", **extra}
    return {**params, "signature": _sign(params)}


async def _make_recommendation(**overrides):
    conversation_id = f"pytest-preview-{uuid.uuid4().hex[:8]}"
    defaults = dict(
        id=f"pytest-rec-{uuid.uuid4().hex[:8]}",
        conversationId=conversation_id,
        customerProfileJson={"likes": ["Rose"]},
        productsJson=[{"title": "Rose Oud", "notes": ["Rose", "Oud"], "contribution": "anchor"}],
        combinationType="HYBRID",
        scoreJson={}, evidenceJson={}, ratiosJson=[{"productTitle": "Rose Oud", "ratioPercent": 100}],
        customerFacingJson={"customerFacingName": "Rose Dream"},
        status="pending", buildStatus="draft",
    )
    defaults.update(overrides)
    from app.db.time import utcnow

    async with SessionLocal() as session:
        record = FragranceRecommendation(createdAt=utcnow(), **defaults)
        session.add(record)
        session.add(Conversation(id=conversation_id, createdAt=utcnow(), updatedAt=utcnow()))
        await session.commit()
    return defaults["id"], conversation_id


async def _cleanup(recommendation_id, conversation_id):
    async with SessionLocal() as session:
        await session.execute(delete(FragranceRecommendation).where(FragranceRecommendation.id == recommendation_id))
        await session.execute(delete(Conversation).where(Conversation.id == conversation_id))
        await session.execute(delete(CustomerProfileState).where(CustomerProfileState.conversationId == conversation_id))
        await session.commit()


@pytest.fixture(autouse=True)
def _shopify_secret(monkeypatch):
    monkeypatch.setattr(settings, "shopify_api_secret", SECRET)


async def test_preview_rejects_missing_app_proxy_signature():
    with TestClient(app) as client:
        response = client.get("/apps/scent-library/fragrance-preview", params={"shop": SHOP, "recommendationId": "x"})
    assert response.status_code == 400


async def test_preview_renders_recommendation_data():
    recommendation_id, conversation_id = await _make_recommendation()
    try:
        with TestClient(app) as client:
            response = client.get("/apps/scent-library/fragrance-preview", params=_proxy_params(recommendationId=recommendation_id))
        assert response.status_code == 200
        assert "Rose Dream" in response.text
        assert recommendation_id in response.text
    finally:
        await _cleanup(recommendation_id, conversation_id)


async def test_preview_asset_urls_stay_under_the_app_proxy_path():
    # The page is only ever loaded through Shopify's App Proxy (test-shop.myshopify.com/apps/
    # scent-library/...) -- an asset URL starting with a bare /static/... resolves, in the
    # browser, against the STOREFRONT's own domain root instead of being forwarded to this
    # backend at all, which is exactly what produced a fully unstyled, non-interactive page live.
    recommendation_id, conversation_id = await _make_recommendation()
    try:
        with TestClient(app) as client:
            response = client.get("/apps/scent-library/fragrance-preview", params=_proxy_params(recommendationId=recommendation_id))
        assert response.status_code == 200
        assert '/apps/scent-library/static/css/fragrance_preview.css' in response.text
        assert '/apps/scent-library/static/js/fragrance_preview.js' in response.text
        assert 'href="/static/css' not in response.text
        assert 'src="/static/js' not in response.text
    finally:
        await _cleanup(recommendation_id, conversation_id)


def test_preview_css_is_reachable_under_the_app_proxy_path_with_correct_content_type():
    with TestClient(app) as client:
        response = client.get("/apps/scent-library/static/css/fragrance_preview.css")
    assert response.status_code == 200
    assert "css" in response.headers["content-type"]


def test_preview_js_is_reachable_under_the_app_proxy_path_with_correct_content_type():
    with TestClient(app) as client:
        response = client.get("/apps/scent-library/static/js/fragrance_preview.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


def test_original_static_mount_still_works_unchanged():
    with TestClient(app) as client:
        response = client.get("/static/css/fragrance_preview.css")
    assert response.status_code == 200
    assert "css" in response.headers["content-type"]


async def test_preview_returns_404_for_unknown_recommendation():
    with TestClient(app) as client:
        response = client.get("/apps/scent-library/fragrance-preview", params=_proxy_params(recommendationId="does-not-exist"))
    assert response.status_code == 404


async def test_preview_recreate_marks_draft_and_flags_profile(monkeypatch):
    recommendation_id, conversation_id = await _make_recommendation()
    try:
        with TestClient(app) as client:
            response = client.post(
                "/apps/scent-library/fragrance-preview", params=_proxy_params(),
                json={"intent": "recreate", "recommendationId": recommendation_id, "name": "New Name", "ratios": {"top": 40, "middle": 30, "base": 30}},
            )
        assert response.status_code == 200
        body = response.json()
        assert body == {"status": "recreate", "redirectUrl": f"https://{SHOP}/"}

        async with SessionLocal() as session:
            from app.services.customer_profile import get_customer_profile

            profile = await get_customer_profile(session, conversation_id)
        assert profile["pendingRecreateRecommendationId"] == recommendation_id
    finally:
        await _cleanup(recommendation_id, conversation_id)


async def test_preview_save_build_first_time_creation(monkeypatch):
    recommendation_id, conversation_id = await _make_recommendation()
    try:
        async def _fake_create(*a, **kw):
            return {"productId": "gid://shopify/Product/1", "variantId": "gid://shopify/ProductVariant/1", "price": 60.0, "productUrl": f"https://{SHOP}/products/rose-dream"}

        monkeypatch.setattr(preview_module, "create_shopify_build_product", _fake_create)

        with TestClient(app) as client:
            response = client.post(
                "/apps/scent-library/fragrance-preview", params=_proxy_params(),
                json={"intent": "save_build", "recommendationId": recommendation_id, "name": "Rose Dream", "ratios": {"top": 40, "middle": 30, "base": 30}},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "saved"
        assert body["shopifyProductId"] == "gid://shopify/Product/1"
        assert body["productUrl"] == f"https://{SHOP}/products/rose-dream"
    finally:
        await _cleanup(recommendation_id, conversation_id)


async def test_preview_add_to_cart_uses_existing_product(monkeypatch):
    recommendation_id, conversation_id = await _make_recommendation(shopifyProductId="gid://shopify/Product/9", shopifyVariantId="gid://shopify/ProductVariant/9")
    try:
        async def _fake_reprice(*a, **kw):
            return {"price": "60.00", "variantId": "gid://shopify/ProductVariant/42", "created": True}

        async def _fake_handle(*a, **kw):
            return "rose-dream"

        monkeypatch.setattr(preview_module, "reprice_existing_build", _fake_reprice)
        monkeypatch.setattr(preview_module, "get_product_handle", _fake_handle)

        with TestClient(app) as client:
            response = client.post(
                "/apps/scent-library/fragrance-preview", params=_proxy_params(),
                json={"intent": "add_to_cart", "recommendationId": recommendation_id, "ratios": {"top": 40, "middle": 30, "base": 30}},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "added"
        assert body["cartUrl"] == f"https://{SHOP}/cart/42:1"
    finally:
        await _cleanup(recommendation_id, conversation_id)


async def test_preview_save_build_reports_shopify_failure_as_json_error(monkeypatch):
    recommendation_id, conversation_id = await _make_recommendation()
    try:
        async def _boom(*a, **kw):
            raise RuntimeError("Shopify is down")

        monkeypatch.setattr(preview_module, "create_shopify_build_product", _boom)

        with TestClient(app) as client:
            response = client.post(
                "/apps/scent-library/fragrance-preview", params=_proxy_params(),
                json={"intent": "save_build", "recommendationId": recommendation_id, "ratios": {"top": 40, "middle": 30, "base": 30}},
            )
        assert response.status_code == 200
        assert response.json() == {"error": "Failed to save the build."}
    finally:
        await _cleanup(recommendation_id, conversation_id)


async def test_preview_rejects_unknown_intent():
    recommendation_id, conversation_id = await _make_recommendation()
    try:
        with TestClient(app) as client:
            response = client.post(
                "/apps/scent-library/fragrance-preview", params=_proxy_params(),
                json={"intent": "bogus", "recommendationId": recommendation_id},
            )
        assert response.status_code == 400
    finally:
        await _cleanup(recommendation_id, conversation_id)
