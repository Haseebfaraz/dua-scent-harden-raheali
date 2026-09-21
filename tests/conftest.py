import pytest

from app.config import settings
from app.db.session import SessionLocal

# Phase 1 (security): every Shopify Admin call refuses to run without a configured trusted shop.
# Give the whole suite one fake trusted shop so tests that never cared about the shop keep
# working; tests that exercise the boundary itself override this explicitly via monkeypatch.
TEST_TRUSTED_SHOP = "test-shop.myshopify.com"


# Phase 2: the internal Node-adapter routes refuse requests unless INTERNAL_API_KEY is configured
# (fail closed). Give the suite a fake key so internal-route tests exercise the real check.
TEST_INTERNAL_API_KEY = "fake-internal-api-key-for-tests"


@pytest.fixture(autouse=True)
def _trusted_shop_for_tests(monkeypatch):
    if not settings.shopify_shop_domain:
        monkeypatch.setattr(settings, "shopify_shop_domain", TEST_TRUSTED_SHOP)
    if not settings.internal_api_key:
        monkeypatch.setattr(settings, "internal_api_key", TEST_INTERNAL_API_KEY)
    # Every TestClient shares one network identity, so the production abuse limits would trip
    # across the suite. Tests that exercise the limiter set their own thresholds explicitly.
    for name in ("rate_limit_conversation_create_per_ip", "rate_limit_chat_turn_per_conversation", "rate_limit_chat_turn_per_conversation_daily",
                 "rate_limit_chat_turn_per_ip", "rate_limit_history_read_per_conversation", "rate_limit_history_read_per_ip",
                 "rate_limit_security_denied_per_conversation", "rate_limit_security_denied_per_ip"):
        monkeypatch.setattr(settings, name, "100000/60")


@pytest.fixture
async def db_session():
    async with SessionLocal() as session:
        yield session


@pytest.fixture
def inventory_verified(monkeypatch):
    """Phase 5: for tests whose subject is ANOTHER invariant of the Shopify write layer (pricing,
    product identity, authorization ordering). It answers the commerce inventory gate positively
    and records each call. It is opt-in per module, never autouse; the gate itself is tested
    without any bypass in tests/security/test_commerce_inventory.py."""
    calls = []

    async def _verified(session, *, recommendation, ratios, quantity=1):
        calls.append({"recommendationId": getattr(recommendation, "id", None), "ratios": dict(ratios), "quantity": quantity})

    monkeypatch.setattr("app.shopify.builds.require_commerce_inventory", _verified)
    return calls


@pytest.fixture(autouse=True)
def _no_live_odoo(monkeypatch, request):
    """Phase 5: no test may reach the real Odoo endpoint (the default URL is a real host). Tests
    that exercise the HTTP client itself mock httpx and opt out with @pytest.mark.odoo_http."""
    if request.node.get_closest_marker("odoo_http"):
        return
    from app.integrations import odoo_client

    async def _blocked(url):
        raise AssertionError("a test tried to reach the real Odoo endpoint; mock get_inventory_by_skus")

    monkeypatch.setattr(odoo_client, "_get_json", _blocked)
