from types import SimpleNamespace

import pytest

from app.shopify import builds
from app.shopify.builds import (
    BuildProductNotSaved,
    InvalidComputedPrice,
    InvalidRatios,
    ProductPricingNotFound,
    create_shopify_build_product,
    reprice_existing_build,
)
from app.shopify.trusted_shop import UntrustedShopError

# The suite-wide trusted shop (tests/conftest.py).
# Phase 5: these tests are about other invariants; the inventory gate has its own suite.
pytestmark = pytest.mark.usefixtures("inventory_verified")

SHOP = "test-shop.myshopify.com"
PRODUCT_ID = "gid://shopify/Product/1"


def _recommendation(**overrides):
    defaults = dict(
        id="rec_1", combinationType="HYBRID", shopifyProductId=PRODUCT_ID,
        productsJson=[{"title": "Rose Oud", "notes": ["Rose", "Oud"], "contribution": "anchor"}],
        customerProfileJson={"likes": ["Rose"]},
        ratiosJson=[{"productTitle": "Rose Oud", "ratioPercent": 100}],
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.mark.parametrize("ratios", [
    {"top": 10, "middle": 10, "base": 10},
    {"top": -100, "middle": 100, "base": 100},   # sums to 100 but a layer is negative (Phase 1, N1)
    {"top": -10, "middle": 60, "base": 50},
    {"top": 150, "middle": -25, "base": -25},
    {"top": 100, "middle": 100, "base": -100},
])
async def test_create_shopify_build_product_rejects_invalid_compositions_before_any_shopify_call(monkeypatch, ratios):
    async def _never(*a, **kw):
        raise AssertionError("Shopify called with an invalid composition")

    monkeypatch.setattr(builds, "create_product", _never)
    monkeypatch.setattr(builds, "compute_price_per_5ml_by_position", _never)
    with pytest.raises(InvalidRatios):
        await create_shopify_build_product(
            None, SHOP, recommendation=_recommendation(), custom_name="X",
            ratios=ratios, customer_name=None, customer_email=None,
        )


async def test_create_shopify_build_product_happy_path(monkeypatch):
    monkeypatch.setattr(builds, "compute_price_per_5ml_by_position", lambda *a, **kw: _async({"top": 20, "middle": 20, "base": 20}))
    monkeypatch.setattr(builds, "create_product", lambda *a, **kw: _async({"id": "gid://shopify/Product/1", "handle": "custom-blend"}))
    monkeypatch.setattr(builds, "attach_product_media", lambda *a, **kw: _async(None))
    monkeypatch.setattr(builds, "publish_to_all_channels", lambda *a, **kw: _async(None))
    monkeypatch.setattr(builds, "get_default_variant_id", lambda *a, **kw: _async("gid://shopify/ProductVariant/1"))

    captured = {}
    order = []

    async def _fake_set_price(session, shop, product_id, variant_id, price):
        captured["price"] = price
        order.append("price")

    async def _readback(session, shop, product_id):
        order.append("readback")
        return {"id": product_id, "variants": {"edges": [{"node": {"id": "gid://shopify/ProductVariant/1", "price": captured["price"]}}]}}

    async def _activate(session, shop, product_id):
        order.append("activate")

    monkeypatch.setattr(builds, "set_variant_price", _fake_set_price)
    monkeypatch.setattr(builds, "get_product_for_pricing", _readback)  # Phase 5A: every variant must carry the price
    monkeypatch.setattr(builds, "activate_product", _activate)         # Phase 5A: DRAFT until then

    result = await create_shopify_build_product(
        None, SHOP, recommendation=_recommendation(), custom_name="My Blend",
        ratios={"top": 40, "middle": 30, "base": 30}, customer_name="Jane", customer_email="jane@example.com",
    )
    assert order == ["price", "readback", "activate"]

    assert result["productId"] == "gid://shopify/Product/1"
    assert result["variantId"] == "gid://shopify/ProductVariant/1"
    assert result["productUrl"] == f"https://{SHOP}/products/custom-blend"
    assert captured["price"] == f"{result['price']:.2f}"


async def test_create_shopify_build_product_tolerates_media_and_publish_failures(monkeypatch):
    monkeypatch.setattr(builds, "compute_price_per_5ml_by_position", lambda *a, **kw: _async({"top": 20, "middle": 20, "base": 20}))
    monkeypatch.setattr(builds, "create_product", lambda *a, **kw: _async({"id": "gid://shopify/Product/1", "handle": "custom-blend"}))

    async def _boom(*a, **kw):
        raise RuntimeError("shopify hiccup")

    monkeypatch.setattr(builds, "attach_product_media", _boom)
    monkeypatch.setattr(builds, "publish_to_all_channels", _boom)
    monkeypatch.setattr(builds, "get_default_variant_id", lambda *a, **kw: _async("gid://shopify/ProductVariant/9"))
    monkeypatch.setattr(builds, "set_variant_price", lambda *a, **kw: _async(None))
    monkeypatch.setattr(builds, "get_product_for_pricing", lambda *a, **kw: _async({"variants": {"edges": [{"node": {"price": "136.00"}}]}}))
    monkeypatch.setattr(builds, "activate_product", lambda *a, **kw: _async(None))

    result = await create_shopify_build_product(
        None, SHOP, recommendation=_recommendation(), custom_name="My Blend",
        ratios={"top": 34, "middle": 33, "base": 33}, customer_name=None, customer_email=None,
    )
    assert result["productId"] == "gid://shopify/Product/1"
    assert result["variantId"] == "gid://shopify/ProductVariant/9"


async def test_create_shopify_build_product_never_publishes_or_reports_success_without_a_priced_variant(monkeypatch):
    """Phase 5: before, a product whose default variant could not be resolved was still published
    and reported as saved with no price set. Now the price is set before publishing, and a
    created-but-unpriced product is reported as an unconfirmed outcome, never as success."""
    published = []
    monkeypatch.setattr(builds, "compute_price_per_5ml_by_position", lambda *a, **kw: _async({"top": 20, "middle": 20, "base": 20}))
    monkeypatch.setattr(builds, "create_product", lambda *a, **kw: _async({"id": "gid://shopify/Product/1", "handle": "custom-blend"}))
    monkeypatch.setattr(builds, "attach_product_media", lambda *a, **kw: _async(None))
    monkeypatch.setattr(builds, "get_default_variant_id", lambda *a, **kw: _async(None))

    async def _publish(*a, **kw):
        published.append(1)

    monkeypatch.setattr(builds, "publish_to_all_channels", _publish)
    with pytest.raises(builds.BuildWriteAmbiguous) as err:
        await create_shopify_build_product(
            None, SHOP, recommendation=_recommendation(), custom_name="My Blend",
            ratios={"top": 34, "middle": 33, "base": 33}, customer_name=None, customer_email=None,
        )
    assert err.value.product_id == "gid://shopify/Product/1" and published == []


def _product_pricing_payload(variants, recommendation_id="rec_1"):
    # Phase 1: the product must carry the identity of a Scent AI build for THIS recommendation
    # (id, vendor, template, note_composition.recommendationId) or nothing is written.
    return {
        "id": PRODUCT_ID, "title": "Rose Dream", "vendor": "The Dua Brand", "templateSuffix": "custom-scent",
        "metafield": {"value": '{"recommendationId": "%s", "layers": [{"position": "top", "quantityMl": 17.0, "pricePer5ml": 20}, {"position": "middle", "quantityMl": 8.5, "pricePer5ml": 20}, {"position": "base", "quantityMl": 8.5, "pricePer5ml": 20}]}' % recommendation_id},
        "variants": {"edges": variants},
    }


async def test_reprice_existing_build_reuses_variant_within_tolerance(monkeypatch):
    monkeypatch.setattr(builds, "rename_product", lambda *a, **kw: _async(None))
    variants = [{
        "node": {
            "id": "gid://shopify/ProductVariant/1", "price": "60.00",
            "selectedOptions": [
                {"name": "Top Note", "value": "Rose (50%)"}, {"name": "Middle Note", "value": "Amber (25%)"}, {"name": "Base Note", "value": "Musk (25%)"},
            ],
            "inventoryItem": {"id": "gid://shopify/InventoryItem/1", "tracked": False},
        },
    }]
    monkeypatch.setattr(builds, "get_product_for_pricing", lambda *a, **kw: _async(_product_pricing_payload(variants)))

    result = await reprice_existing_build(None, SHOP, recommendation=_recommendation(), ratios={"top": 51, "middle": 24, "base": 25})
    assert result == {"price": "60.00", "variantId": "gid://shopify/ProductVariant/1", "created": False}


async def test_reprice_existing_build_creates_new_variant_outside_tolerance(monkeypatch):
    monkeypatch.setattr(builds, "rename_product", lambda *a, **kw: _async(None))
    variants = [{
        "node": {
            "id": "gid://shopify/ProductVariant/1", "price": "60.00",
            "selectedOptions": [
                {"name": "Top Note", "value": "Rose (50%)"}, {"name": "Middle Note", "value": "Amber (25%)"}, {"name": "Base Note", "value": "Musk (25%)"},
            ],
            "inventoryItem": {"id": "gid://shopify/InventoryItem/1", "tracked": False},
        },
    }]
    monkeypatch.setattr(builds, "get_product_for_pricing", lambda *a, **kw: _async(_product_pricing_payload(variants)))

    captured = {}

    async def _fake_create_variant(session, shop, product_id, price, option_values):
        captured["option_values"] = option_values
        return {"id": "gid://shopify/ProductVariant/2", "price": price}

    monkeypatch.setattr(builds, "create_variant", _fake_create_variant)

    result = await reprice_existing_build(None, SHOP, recommendation=_recommendation(), ratios={"top": 10, "middle": 10, "base": 80})
    assert result["created"] is True
    assert result["variantId"] == "gid://shopify/ProductVariant/2"
    names = {ov["optionName"]: ov["name"] for ov in captured["option_values"]}
    assert names["Base Note"] == "Musk (80%)"


async def test_reprice_existing_build_raises_when_product_pricing_missing(monkeypatch):
    renamed = []
    monkeypatch.setattr(builds, "rename_product", lambda *a, **kw: renamed.append(a) or _async(None))
    monkeypatch.setattr(builds, "get_product_for_pricing", lambda *a, **kw: _async(None))
    with pytest.raises(ProductPricingNotFound):
        await reprice_existing_build(None, SHOP, recommendation=_recommendation(), ratios={"top": 50, "middle": 25, "base": 25}, name="New Name")
    assert renamed == []  # Phase 1: no rename before the product is verified


async def test_reprice_existing_build_raises_on_invalid_price(monkeypatch):
    renamed = []
    monkeypatch.setattr(builds, "rename_product", lambda *a, **kw: renamed.append(a) or _async(None))
    payload = _product_pricing_payload([{"node": {"id": "v1", "price": "0", "selectedOptions": [], "inventoryItem": {}}}])
    payload["metafield"] = {"value": '{"recommendationId": "rec_1", "layers": [{"position": "top", "quantityMl": 0, "pricePer5ml": 0}, {"position": "middle", "quantityMl": 0, "pricePer5ml": 0}, {"position": "base", "quantityMl": 0, "pricePer5ml": 0}]}'}
    monkeypatch.setattr(builds, "get_product_for_pricing", lambda *a, **kw: _async(payload))
    with pytest.raises(InvalidComputedPrice):
        await reprice_existing_build(None, SHOP, recommendation=_recommendation(), ratios={"top": 50, "middle": 25, "base": 25}, name="New Name")
    assert renamed == []


async def test_reprice_existing_build_refuses_a_product_that_belongs_to_another_recommendation(monkeypatch):
    renamed = []
    monkeypatch.setattr(builds, "rename_product", lambda *a, **kw: renamed.append(a) or _async(None))
    monkeypatch.setattr(builds, "create_variant", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("variant created for a foreign product")))
    payload = _product_pricing_payload([{"node": {"id": "v1", "price": "60.00", "selectedOptions": [], "inventoryItem": {}}}], recommendation_id="someone-elses-rec")
    monkeypatch.setattr(builds, "get_product_for_pricing", lambda *a, **kw: _async(payload))
    with pytest.raises(ProductPricingNotFound):
        await reprice_existing_build(None, SHOP, recommendation=_recommendation(), ratios={"top": 50, "middle": 25, "base": 25}, name="New Name")
    assert renamed == []


async def test_reprice_existing_build_requires_a_saved_product(monkeypatch):
    monkeypatch.setattr(builds, "get_product_for_pricing", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("Shopify read for an unsaved build")))
    with pytest.raises(BuildProductNotSaved):
        await reprice_existing_build(None, SHOP, recommendation=_recommendation(shopifyProductId=None), ratios={"top": 50, "middle": 25, "base": 25})


async def test_reprice_existing_build_refuses_an_untrusted_shop_before_any_read(monkeypatch):
    monkeypatch.setattr(builds, "get_product_for_pricing", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("Shopify read for an untrusted shop")))
    with pytest.raises(UntrustedShopError):
        await reprice_existing_build(None, "attacker.example.com", recommendation=_recommendation(), ratios={"top": 50, "middle": 25, "base": 25})


async def _async(value):
    return value
