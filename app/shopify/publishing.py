"""Port of the publish-to-all-sales-channels step in fragranceBuild.server.js's
createShopifyBuildProduct. A failure here is logged and swallowed by the caller, matching the JS
original -- a product that failed to publish is still a real, usable product.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.shopify.admin_client import admin_graphql


async def publish_to_all_channels(session: AsyncSession, shop: str, product_id: str) -> None:
    publications_result = await admin_graphql(session, shop, "query getPublications { publications(first: 25) { nodes { id } } }")
    publication_ids = [n["id"] for n in (publications_result.get("data", {}).get("publications") or {}).get("nodes", [])]
    if not publication_ids:
        return
    await admin_graphql(
        session, shop,
        """
        mutation publishToAllChannels($id: ID!, $input: [PublicationInput!]!) {
          publishablePublish(id: $id, input: $input) { userErrors { field message } }
        }
        """,
        {"id": product_id, "input": [{"publicationId": pub_id} for pub_id in publication_ids]},
    )
