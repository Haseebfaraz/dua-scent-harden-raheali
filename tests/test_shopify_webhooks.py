import base64
import hashlib
import hmac as _hmac
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db.models import Session as ShopifySession
from app.db.session import SessionLocal
from app.main import app

SECRET = "webhook-test-secret"


def _sign(body: bytes, secret: str) -> str:
    return base64.b64encode(_hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def test_webhook_rejects_invalid_signature(monkeypatch):
    monkeypatch.setattr(settings, "shopify_api_secret", SECRET)
    with TestClient(app) as client:
        response = client.post(
            "/shopify/webhooks", content=b"{}",
            headers={"X-Shopify-Topic": "app/uninstalled", "X-Shopify-Shop-Domain": "x.myshopify.com", "X-Shopify-Hmac-Sha256": "wrong"},
        )
    assert response.status_code == 401


def test_webhook_rejects_unhandled_topic(monkeypatch):
    monkeypatch.setattr(settings, "shopify_api_secret", SECRET)
    body = b"{}"
    with TestClient(app) as client:
        response = client.post(
            "/shopify/webhooks", content=body,
            headers={
                "X-Shopify-Topic": "orders/create", "X-Shopify-Shop-Domain": "x.myshopify.com",
                "X-Shopify-Hmac-Sha256": _sign(body, SECRET),
            },
        )
    assert response.status_code == 404


async def test_app_uninstalled_deletes_the_shop_session(monkeypatch):
    monkeypatch.setattr(settings, "shopify_api_secret", SECRET)
    shop = f"pytest-uninstall-{uuid.uuid4().hex[:8]}.myshopify.com"

    async with SessionLocal() as db:
        db.add(ShopifySession(id=uuid.uuid4().hex, shop=shop, state="x", isOnline=False, accessToken="shpat_x"))
        await db.commit()

    body = b"{}"
    with TestClient(app) as client:
        response = client.post(
            "/shopify/webhooks", content=body,
            headers={
                "X-Shopify-Topic": "app/uninstalled", "X-Shopify-Shop-Domain": shop,
                "X-Shopify-Hmac-Sha256": _sign(body, SECRET),
            },
        )
    assert response.status_code == 200

    async with SessionLocal() as db:
        remaining = (await db.execute(select(ShopifySession).where(ShopifySession.shop == shop))).first()
    assert remaining is None
