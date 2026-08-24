"""Metafield value builders -- pure functions, no HTTP. Port of the metafields array
fragranceBuild.server.js's createShopifyBuildProduct passes to productCreate, plus the
metafield-definition mutation api.customer-builds.jsx uses so its `query:` search actually
filters (Shopify only indexes a metafield for search once a formal definition exists).
"""

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.shopify.admin_client import admin_graphql


def build_note_composition_metafield(recommendation_id: str, combination_type: str, layers: list[dict]) -> dict[str, Any]:
    return {
        "namespace": "custom", "key": "note_composition", "type": "json",
        "value": json.dumps({"recommendationId": recommendation_id, "combinationType": combination_type, "layers": layers}),
    }


def build_internal_components_metafield(internal_products: list[dict]) -> dict[str, Any]:
    return {
        "namespace": "custom", "key": "internal_components", "type": "json",
        "value": json.dumps([{"title": p.get("title"), "contribution": p.get("contribution")} for p in (internal_products or [])]),
    }


def build_customer_identity_metafields(customer_name: str | None, customer_email: str | None) -> list[dict[str, Any]]:
    return [
        {"namespace": "custom", "key": "customer_name", "type": "single_line_text_field", "value": customer_name or ""},
        {"namespace": "custom", "key": "customer_email", "type": "single_line_text_field", "value": customer_email or ""},
    ]


async def ensure_customer_email_definition(session: AsyncSession, shop: str) -> None:
    """Idempotent -- Shopify errors if the definition already exists; that error is expected and
    ignored, matching the reference app exactly."""
    await admin_graphql(
        session, shop,
        """
        mutation ensureCustomerEmailDefinition($definition: MetafieldDefinitionInput!) {
          metafieldDefinitionCreate(definition: $definition) {
            createdDefinition { id }
            userErrors { field message code }
          }
        }
        """,
        {"definition": {
            "name": "Customer Email", "namespace": "custom", "key": "customer_email",
            "type": "single_line_text_field", "ownerType": "PRODUCT",
        }},
    )
