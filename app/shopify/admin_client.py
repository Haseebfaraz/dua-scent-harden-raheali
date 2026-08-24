"""Raw Admin GraphQL calls via httpx -- no Shopify SDK needed, this is just an authenticated
POST. Mirrors what Node's `admin.graphql(query, {variables})` does under the hood.
"""

from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession as DbSession

from app.config import settings
from app.shopify.sessions import get_offline_access_token

_REQUEST_TIMEOUT_SECONDS = 15.0


class ShopNotAuthenticated(Exception):
    """No stored offline token for this shop -- the merchant hasn't installed, or Session was
    cleared (e.g. by the APP_UNINSTALLED webhook handler)."""


async def _post(url: str, token: str, query: str, variables: dict[str, Any]) -> httpx.Response:
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
        return await client.post(
            url,
            json={"query": query, "variables": variables},
            headers={"Content-Type": "application/json", "X-Shopify-Access-Token": token},
        )


async def admin_graphql(session: DbSession, shop: str, query: str, variables: dict[str, Any] | None = None) -> dict:
    token = await get_offline_access_token(session, shop)
    if not token:
        raise ShopNotAuthenticated(f'no stored offline access token for shop "{shop}"')

    url = f"https://{shop}/admin/api/{settings.shopify_api_version}/graphql.json"
    response = await _post(url, token, query, variables or {})
    response.raise_for_status()
    return response.json()
