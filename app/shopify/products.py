"""Admin GraphQL product/variant/media primitives -- port of the mutations/queries
app/services/fragranceBuild.server.js and app/routes/api.save-build.jsx use directly, kept
separate from that build-specific orchestration (see app/shopify/builds.py) and from
recommendation logic. Mutations/queries are byte-identical to the Node reference; only the
transport (httpx instead of admin.graphql()) changed.
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
    result = await admin_graphql(
        session, shop,
        """
        mutation createProduct($input: ProductInput!) {
          productCreate(input: $input) {
            product { id handle }
            userErrors { field message }
          }
        }
        """,
        {"input": {
            "title": title, "descriptionHtml": description_html, "vendor": vendor, "status": "ACTIVE",
            "templateSuffix": template_suffix, "productOptions": product_options, "metafields": metafields,
        }},
    )
    payload = result.get("data", {}).get("productCreate") or {}
    product, errors = payload.get("product"), payload.get("userErrors")
    if not product or errors:
        raise ShopifyGraphqlError(", ".join(e["message"] for e in errors) if errors else "Product creation failed.")
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
    edges = (result.get("data", {}).get("product") or {}).get("variants", {}).get("edges", [])
    return edges[0]["node"]["id"] if edges else None


async def set_variant_price(session: AsyncSession, shop: str, product_id: str, variant_id: str, price: str, tracked: bool = False) -> None:
    await admin_graphql(
        session, shop,
        """
        mutation setPrice($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
          productVariantsBulkUpdate(productId: $productId, variants: $variants) {
            product { id }
            userErrors { field message }
          }
        }
        """,
        {"productId": product_id, "variants": [{"id": variant_id, "price": price, "inventoryItem": {"tracked": tracked}}]},
    )


async def get_product_handle(session: AsyncSession, shop: str, product_id: str) -> str | None:
    result = await admin_graphql(session, shop, "query getProductHandle($id: ID!) { product(id: $id) { handle } }", {"id": product_id})
    return (result.get("data", {}).get("product") or {}).get("handle")


async def rename_product(session: AsyncSession, shop: str, product_id: str, name: str) -> None:
    if not isinstance(name, str) or not name.strip():
        return
    await admin_graphql(
        session, shop,
        "mutation renameBuildProduct($input: ProductInput!) { productUpdate(input: $input) { userErrors { field message } } }",
        {"input": {"id": product_id, "title": name.strip()}},
    )


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
    payload = result.get("data", {}).get("productVariantsBulkCreate") or {}
    errors = payload.get("userErrors")
    if errors:
        raise ShopifyGraphqlError(", ".join(e["message"] for e in errors))
    return payload["productVariants"][0]


async def set_inventory_item_untracked(session: AsyncSession, shop: str, inventory_item_id: str) -> None:
    if not inventory_item_id:
        return
    await admin_graphql(
        session, shop,
        "mutation untrackInventoryItem($id: ID!, $input: InventoryItemInput!) { inventoryItemUpdate(id: $id, input: $input) { userErrors { message } } }",
        {"id": inventory_item_id, "input": {"tracked": False}},
    )
