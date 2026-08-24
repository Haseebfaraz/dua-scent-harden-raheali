import pytest

from app.shopify import products
from app.shopify.products import (
    ShopifyGraphqlError,
    attach_product_media,
    create_product,
    create_variant,
    get_default_variant_id,
    get_product_for_pricing,
    get_product_handle,
    rename_product,
    set_inventory_item_untracked,
    set_variant_price,
)

SHOP = "test-shop.myshopify.com"


def _mock_graphql(monkeypatch, response, capture=None):
    async def _fake(session, shop, query, variables=None):
        if capture is not None:
            capture.append({"shop": shop, "query": query, "variables": variables})
        return response

    monkeypatch.setattr(products, "admin_graphql", _fake)


async def test_create_product_returns_product_on_success(monkeypatch):
    _mock_graphql(monkeypatch, {"data": {"productCreate": {"product": {"id": "gid://shopify/Product/1", "handle": "x"}, "userErrors": []}}})
    result = await create_product(
        None, SHOP, title="Custom Blend", description_html="<p>x</p>", vendor="The Dua Brand",
        template_suffix="custom-scent", product_options=[], metafields=[],
    )
    assert result == {"id": "gid://shopify/Product/1", "handle": "x"}


async def test_create_product_raises_on_user_errors(monkeypatch):
    _mock_graphql(monkeypatch, {"data": {"productCreate": {"product": None, "userErrors": [{"field": "title", "message": "bad title"}]}}})
    with pytest.raises(ShopifyGraphqlError, match="bad title"):
        await create_product(None, SHOP, title="", description_html="", vendor="", template_suffix="", product_options=[], metafields=[])


async def test_get_default_variant_id_returns_first_edge(monkeypatch):
    _mock_graphql(monkeypatch, {"data": {"product": {"variants": {"edges": [{"node": {"id": "gid://shopify/ProductVariant/9"}}]}}}})
    assert await get_default_variant_id(None, SHOP, "gid://shopify/Product/1") == "gid://shopify/ProductVariant/9"


async def test_get_default_variant_id_returns_none_when_no_variants(monkeypatch):
    _mock_graphql(monkeypatch, {"data": {"product": {"variants": {"edges": []}}}})
    assert await get_default_variant_id(None, SHOP, "gid://shopify/Product/1") is None


async def test_get_product_handle(monkeypatch):
    _mock_graphql(monkeypatch, {"data": {"product": {"handle": "custom-blend-1"}}})
    assert await get_product_handle(None, SHOP, "gid://shopify/Product/1") == "custom-blend-1"


async def test_rename_product_skips_blank_name(monkeypatch):
    calls = []
    _mock_graphql(monkeypatch, {"data": {}}, capture=calls)
    await rename_product(None, SHOP, "gid://shopify/Product/1", "   ")
    assert calls == []


async def test_rename_product_sends_trimmed_title(monkeypatch):
    calls = []
    _mock_graphql(monkeypatch, {"data": {"productUpdate": {"userErrors": []}}}, capture=calls)
    await rename_product(None, SHOP, "gid://shopify/Product/1", "  My Blend  ")
    assert calls[0]["variables"]["input"]["title"] == "My Blend"


async def test_get_product_for_pricing_returns_product_payload(monkeypatch):
    payload = {"metafield": {"value": "{}"}, "variants": {"edges": []}}
    _mock_graphql(monkeypatch, {"data": {"product": payload}})
    assert await get_product_for_pricing(None, SHOP, "gid://shopify/Product/1") == payload


async def test_create_variant_returns_new_variant(monkeypatch):
    _mock_graphql(monkeypatch, {"data": {"productVariantsBulkCreate": {"userErrors": [], "productVariants": [{"id": "gid://shopify/ProductVariant/2", "price": "10.00"}]}}})
    result = await create_variant(None, SHOP, "gid://shopify/Product/1", "10.00", [])
    assert result == {"id": "gid://shopify/ProductVariant/2", "price": "10.00"}


async def test_create_variant_raises_on_user_errors(monkeypatch):
    _mock_graphql(monkeypatch, {"data": {"productVariantsBulkCreate": {"userErrors": [{"field": "price", "message": "invalid price"}], "productVariants": []}}})
    with pytest.raises(ShopifyGraphqlError, match="invalid price"):
        await create_variant(None, SHOP, "gid://shopify/Product/1", "-1", [])


async def test_set_inventory_item_untracked_skips_none(monkeypatch):
    calls = []
    _mock_graphql(monkeypatch, {}, capture=calls)
    await set_inventory_item_untracked(None, SHOP, None)
    assert calls == []


async def test_attach_product_media_and_set_variant_price_do_not_raise(monkeypatch):
    _mock_graphql(monkeypatch, {"data": {}})
    await attach_product_media(None, SHOP, "gid://shopify/Product/1", "https://example.com/x.png", "alt")
    await set_variant_price(None, SHOP, "gid://shopify/Product/1", "gid://shopify/ProductVariant/1", "19.99")
