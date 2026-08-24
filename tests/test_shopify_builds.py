from types import SimpleNamespace

import pytest

from app.shopify import builds
from app.shopify.builds import (
    InvalidComputedPrice,
    InvalidRatios,
    ProductPricingNotFound,
    create_shopify_build_product,
    reprice_existing_build,
)

SHOP = "test-shop.myshopify.com"


def _recommendation(**overrides):
    defaults = dict(
        id="rec_1", combinationType="HYBRID",
        productsJson=[{"title": "Rose Oud", "notes": ["Rose", "Oud"], "contribution": "anchor"}],
        customerProfileJson={"likes": ["Rose"]},
        ratiosJson=[{"productTitle": "Rose Oud", "ratioPercent": 100}],
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


async def test_create_shopify_build_product_rejects_ratios_not_summing_to_100():
    with pytest.raises(InvalidRatios):
        await create_shopify_build_product(
            None, SHOP, recommendation=_recommendation(), custom_name="X",
            ratios={"top": 10, "middle": 10, "base": 10}, customer_name=None, customer_email=None,
        )


async def test_create_shopify_build_product_happy_path(monkeypatch):
    monkeypatch.setattr(builds, "compute_price_per_5ml_by_position", lambda *a, **kw: _async({"top": 20, "middle": 20, "base": 20}))
    monkeypatch.setattr(builds, "create_product", lambda *a, **kw: _async({"id": "gid://shopify/Product/1", "handle": "custom-blend"}))
    monkeypatch.setattr(builds, "attach_product_media", lambda *a, **kw: _async(None))
    monkeypatch.setattr(builds, "publish_to_all_channels", lambda *a, **kw: _async(None))
    monkeypatch.setattr(builds, "get_default_variant_id", lambda *a, **kw: _async("gid://shopify/ProductVariant/1"))

    captured = {}

    async def _fake_set_price(session, shop, product_id, variant_id, price):
        captured["price"] = price

    monkeypatch.setattr(builds, "set_variant_price", _fake_set_price)

    result = await create_shopify_build_product(
        None, SHOP, recommendation=_recommendation(), custom_name="My Blend",
        ratios={"top": 40, "middle": 30, "base": 30}, customer_name="Jane", customer_email="jane@example.com",
    )

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
    monkeypatch.setattr(builds, "get_default_variant_id", lambda *a, **kw: _async(None))

    result = await create_shopify_build_product(
        None, SHOP, recommendation=_recommendation(), custom_name="My Blend",
        ratios={"top": 34, "middle": 33, "base": 33}, customer_name=None, customer_email=None,
    )
    assert result["productId"] == "gid://shopify/Product/1"
    assert result["variantId"] is None


def _product_pricing_payload(variants):
    return {
        "metafield": {"value": '{"layers": [{"position": "top", "quantityMl": 17.0, "pricePer5ml": 20}, {"position": "middle", "quantityMl": 8.5, "pricePer5ml": 20}, {"position": "base", "quantityMl": 8.5, "pricePer5ml": 20}]}'},
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

    result = await reprice_existing_build(None, SHOP, product_id="gid://shopify/Product/1", ratios={"top": 51, "middle": 24, "base": 25})
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

    result = await reprice_existing_build(None, SHOP, product_id="gid://shopify/Product/1", ratios={"top": 10, "middle": 10, "base": 80})
    assert result["created"] is True
    assert result["variantId"] == "gid://shopify/ProductVariant/2"
    names = {ov["optionName"]: ov["name"] for ov in captured["option_values"]}
    assert names["Base Note"] == "Musk (80%)"


async def test_reprice_existing_build_raises_when_product_pricing_missing(monkeypatch):
    monkeypatch.setattr(builds, "rename_product", lambda *a, **kw: _async(None))
    monkeypatch.setattr(builds, "get_product_for_pricing", lambda *a, **kw: _async(None))
    with pytest.raises(ProductPricingNotFound):
        await reprice_existing_build(None, SHOP, product_id="gid://shopify/Product/1", ratios={"top": 50, "middle": 25, "base": 25})


async def test_reprice_existing_build_raises_on_invalid_price(monkeypatch):
    monkeypatch.setattr(builds, "rename_product", lambda *a, **kw: _async(None))
    payload = {"metafield": {"value": '{"layers": []}'}, "variants": {"edges": [{"node": {"id": "v1", "price": "0", "selectedOptions": [], "inventoryItem": {}}}]}}
    monkeypatch.setattr(builds, "get_product_for_pricing", lambda *a, **kw: _async(payload))
    with pytest.raises(InvalidComputedPrice):
        await reprice_existing_build(None, SHOP, product_id="gid://shopify/Product/1", ratios={"top": 50, "middle": 25, "base": 25})


async def _async(value):
    return value
