"""Liveness stays dependency-free; readiness checks configuration, connectivity and schema and
reveals only check names."""

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def test_liveness_is_unchanged():
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok", "service": "dua-scent-ai-python"}


def test_ready_when_configured_and_migrated(monkeypatch):
    monkeypatch.setattr(settings, "shopify_api_key", "k")
    monkeypatch.setattr(settings, "shopify_api_secret", "s")
    with TestClient(app) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200 and response.json() == {"status": "ready", "failing": []}


def test_not_ready_names_failing_checks_without_values(monkeypatch):
    monkeypatch.setattr(settings, "shopify_shop_domain", "")
    monkeypatch.setattr(settings, "shopify_api_key", "")
    monkeypatch.setattr(settings, "shopify_api_secret", "super-secret-value")
    with TestClient(app) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["failing"] == ["config.shopify_app_credentials", "config.trusted_shop"]
    assert "super-secret-value" not in response.text and settings.database_url not in response.text
