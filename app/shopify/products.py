"""Admin GraphQL product/variant/media primitives -- port of the mutations/queries
app/services/fragranceBuild.server.js and app/routes/api.save-build.jsx use directly, kept
separate from that build-specific orchestration (see app/shopify/builds.py) and from
recommendation logic. Phase 7 (F10): every mutation checks its userErrors and its returned result, and productCreate /
productUpdate use the non-deprecated `product:` argument (see docs/PLATFORM_MODERNIZATION.md).
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.shopify.admin_client import admin_graphql

BOTTLE_IMAGE_URL = "https://cdn.shopify.com/s/files/1/1005/4379/1236/files/animated_bottle.png?v=1784530062"


class ShopifyGraphqlError(Exception):
    """A GraphQL call returned userErrors, or the expected data shape was missing."""


async def create_product(
    session: AsyncSession, shop: str, *, title: str, description_html: str, vendor: str,
    template_suffix: str, product_options: list[dict], metafields: list[dict],
) -> dict[str, str]:
    # Phase 7 (F10): the `product: ProductCreateInput` argument. The old `input: ProductInput`
    # argument is deprecated in every supported version and may be removed from a future one.
    # Field names are identical.
    result = await admin_graphql(
        session, shop,
        """
        mutation createProduct($product: ProductCreateInput!) {
          productCreate(product: $product) {
            product { id handle status }
            userErrors { field message }
          }
        }
        """,
        {"product": {
            # Phase 5A: created as DRAFT. A draft product cannot be bought on any channel, whatever
            # its publication state, so nothing is purchasable until builds.py has set and verified
            # the price and calls activate_product.
            "title": title, "descriptionHtml": description_html, "vendor": vendor, "status": "DRAFT",
            "templateSuffix": template_suffix, "productOptions": product_options, "metafields": metafields,
        }},
    )
    payload = (result.get("data") or {}).get("productCreate") or {}
    product, errors = payload.get("product"), payload.get("userErrors")
    if not product or errors or not isinstance(product.get("id"), str) or not product.get("handle"):
        raise ShopifyGraphqlError("product_create_rejected" if errors else "product_create_missing_result")
    if product.get("status") != "DRAFT":
        # Defense in depth for Phase 5A: the build must not exist as a purchasable product yet.
        raise ShopifyGraphqlError("product_create_unexpected_status")
    return product


async def attach_product_media(session: AsyncSession, shop: str, product_id: str, image_url: str, alt: str) -> None:
    await admin_graphql(
        session, shop,
        """
        mutation attachBottleImage($productId: ID!, $media: [CreateMediaInput!]!) {
          productCreateMedia(productId: $productId, media: $media) { mediaUserErrors { field message } }
        }
        """,
        {"productId": product_id, "media": [{"mediaContentType": "IMAGE", "originalSource": image_url, "alt": alt}]},
    )


async def get_default_variant_id(session: AsyncSession, shop: str, product_id: str) -> str | None:
    result = await admin_graphql(
        session, shop,
        "query getVariants($id: ID!) { product(id: $id) { variants(first: 1) { edges { node { id } } } } }",
        {"id": product_id},
    )
    edges = ((result.get("data") or {}).get("product") or {}).get("variants", {}).get("edges", [])
    return edges[0]["node"]["id"] if edges else None


async def set_variant_price(session: AsyncSession, shop: str, product_id: str, variant_id: str, price: str, tracked: bool = False) -> None:
    """Sets the price only. No `inventoryQuantities` are ever sent, so the 2026-04 inventory
    `changeFromQuantity` requirement does not apply to this app (docs/PLATFORM_MODERNIZATION.md)."""
    result = await admin_graphql(
        session, shop,
        """
        mutation setPrice($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
          productVariantsBulkUpdate(productId: $productId, variants: $variants) {
            productVariants { id price }
            userErrors { field message }
          }
        }
        """,
        {"productId": product_id, "variants": [{"id": variant_id, "price": price, "inventoryItem": {"tracked": tracked}}]},
    )
    payload = (result.get("data") or {}).get("productVariantsBulkUpdate") or {}
    variants = payload.get("productVariants") or []
    if payload.get("userErrors") or not variants or variants[0].get("id") != variant_id:
        raise ShopifyGraphqlError("variant_price_rejected" if payload.get("userErrors") else "variant_price_missing_result")  # Phase 7: was silently ignored


async def get_product_handle(session: AsyncSession, shop: str, product_id: str) -> str | None:
    result = await admin_graphql(session, shop, "query getProductHandle($id: ID!) { product(id: $id) { handle } }", {"id": product_id})
    return ((result.get("data") or {}).get("product") or {}).get("handle")


async def activate_product(session: AsyncSession, shop: str, product_id: str) -> None:
    """DRAFT -> ACTIVE. Unlike rename_product this checks userErrors: a build must never be
    treated as purchasable-and-complete if Shopify refused to activate it."""
    result = await admin_graphql(
        session, shop,
        "mutation activateBuildProduct($product: ProductUpdateInput!) { productUpdate(product: $product) { product { id status } userErrors { field message } } }",
        {"product": {"id": product_id, "status": "ACTIVE"}},
    )
    payload = (result.get("data") or {}).get("productUpdate") or {}
    if payload.get("userErrors") or (payload.get("product") or {}).get("status") != "ACTIVE":
        raise ShopifyGraphqlError("product_activation_not_confirmed")


async def rename_product(session: AsyncSession, shop: str, product_id: str, name: str) -> None:
    if not isinstance(name, str) or not name.strip():
        return
    result = await admin_graphql(
        session, shop,
        "mutation renameBuildProduct($product: ProductUpdateInput!) { productUpdate(product: $product) { userErrors { field message } } }",
        {"product": {"id": product_id, "title": name.strip()}},
    )
    if ((result.get("data") or {}).get("productUpdate") or {}).get("userErrors"):
        raise ShopifyGraphqlError("product_rename_rejected")  # Phase 7: userErrors were ignored before


async def get_product_for_pricing(session: AsyncSession, shop: str, product_id: str) -> dict[str, Any] | None:
    result = await admin_graphql(
        session, shop,
        """
        query getProductForPricing($id: ID!) {
          product(id: $id) {
            id
            title
            vendor
            templateSuffix
            metafield(namespace: "custom", key: "note_composition") { value }
            variants(first: 100) {
              edges { node { id price selectedOptions { name value } inventoryItem { id tracked } } }
            }
          }
        }
        """,
        {"id": product_id},
    )
    return result.get("data", {}).get("product")


async def create_variant(session: AsyncSession, shop: str, product_id: str, price: str, option_values: list[dict], tracked: bool = False) -> dict[str, Any]:
    result = await admin_graphql(
        session, shop,
        """
        mutation createBuildVariant($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
          productVariantsBulkCreate(productId: $productId, variants: $variants) {
            userErrors { field message }
            productVariants { id price }
          }
        }
        """,
        {"productId": product_id, "variants": [{"price": price, "optionValues": option_values, "inventoryItem": {"tracked": tracked}}]},
    )
    payload = (result.get("data") or {}).get("productVariantsBulkCreate") or {}
    errors = payload.get("userErrors")
    variants = payload.get("productVariants") or []
    if errors or not variants or not isinstance(variants[0].get("id"), str) or variants[0].get("price") is None:
        raise ShopifyGraphqlError("variant_create_rejected" if errors else "variant_create_missing_result")
    return variants[0]


async def set_inventory_item_untracked(session: AsyncSession, shop: str, inventory_item_id: str) -> None:
    if not inventory_item_id:
        return
    result = await admin_graphql(
        session, shop,
        "mutation untrackInventoryItem($id: ID!, $input: InventoryItemInput!) { inventoryItemUpdate(id: $id, input: $input) { inventoryItem { id tracked } userErrors { field message } } }",
        {"id": inventory_item_id, "input": {"tracked": False}},
    )
    payload = (result.get("data") or {}).get("inventoryItemUpdate") or {}
    if payload.get("userErrors") or (payload.get("inventoryItem") or {}).get("tracked") is not False:
        raise ShopifyGraphqlError("inventory_item_untrack_rejected")  # Phase 7: was silently ignored
