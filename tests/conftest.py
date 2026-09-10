import pytest

from app.config import settings
from app.db.session import SessionLocal

# Phase 1 (security): every Shopify Admin call refuses to run without a configured trusted shop.
# Give the whole suite one fake trusted shop so tests that never cared about the shop keep
# working; tests that exercise the boundary itself override this explicitly via monkeypatch.
TEST_TRUSTED_SHOP = "test-shop.myshopify.com"


@pytest.fixture(autouse=True)
def _trusted_shop_for_tests(monkeypatch):
    if not settings.shopify_shop_domain:
        monkeypatch.setattr(settings, "shopify_shop_domain", TEST_TRUSTED_SHOP)


@pytest.fixture
async def db_session():
    async with SessionLocal() as session:
        yield session
