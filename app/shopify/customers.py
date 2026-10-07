"""Read a Shopify customer's contact details server-side, by the id Shopify itself signed.

Requires the `read_customers` scope and Shopify's protected-customer-data access for the name and
email fields. Any failure (scope missing, access not approved, transport error) returns None: the
caller treats contact data as unavailable, never as an error the customer sees.
"""

import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.shopify.admin_client import admin_graphql

logger = logging.getLogger(__name__)

_QUERY = "query customerContact($id: ID!) { customer(id: $id) { firstName email } }"


async def fetch_customer_contact(session: AsyncSession, shop: str, customer_id: str) -> dict | None:
    try:
        payload = await admin_graphql(session, shop, _QUERY, {"id": f"gid://shopify/Customer/{customer_id}"})
    except Exception as err:  # noqa: BLE001 -- unavailable, by type only (messages can carry payloads)
        logger.info("SHOPIFY_CUSTOMER_CONTACT_UNAVAILABLE %s", json.dumps({"errorType": type(err).__name__}))
        return None
    customer = (payload.get("data") or {}).get("customer")
    if not isinstance(customer, dict):
        logger.info("SHOPIFY_CUSTOMER_CONTACT_UNAVAILABLE %s", json.dumps({"errorType": "customer_not_returned"}))
        return None
    return {"firstName": customer.get("firstName"), "email": customer.get("email")}
