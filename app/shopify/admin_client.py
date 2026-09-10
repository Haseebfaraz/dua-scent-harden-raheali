"""Raw Admin GraphQL calls via httpx -- no Shopify SDK needed, this is just an authenticated
POST. Mirrors what Node's `admin.graphql(query, {variables})` does under the hood.
"""

from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession as DbSession

from app.config import settings
from app.shopify.admin_auth import get_admin_access_token
from app.shopify.trusted_shop import require_trusted_shop

_REQUEST_TIMEOUT_SECONDS = 15.0


class ShopNotAuthenticated(Exception):
    """No usable Admin API credential for this shop -- client credentials aren't configured/
    available and no stored offline Session token exists either."""


async def _post(url: str, token: str, query: str, variables: dict[str, Any]) -> httpx.Response:
    # follow_redirects=False (httpx default) stated explicitly: this request carries the Admin
    # access token and must never be replayed to a redirect target.
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=False) as client:
        return await client.post(
            url,
            json={"query": query, "variables": variables},
            headers={"Content-Type": "application/json", "X-Shopify-Access-Token": token},
        )


async def admin_graphql(session: DbSession, shop: str, query: str, variables: dict[str, Any] | None = None) -> dict:
    # Phase 1 (F1): an access token is about to be sent to https://{shop}/... -- the destination
    # MUST be the configured trusted shop. Raises before any credential lookup or HTTP request.
    shop = require_trusted_shop(shop)

    # Client credentials grant first (cached, auto-refreshed) -- only falls back to a stored
    # Session-table token when client credentials aren't configured/available. Never prefers a
    # known-stale Session token over a fresh attempt.
    token, _source = await get_admin_access_token(session, shop)
    if not token:
        raise ShopNotAuthenticated(f'no usable Admin API credential for shop "{shop}"')

    url = f"https://{shop}/admin/api/{settings.shopify_api_version}/graphql.json"
    response = await _post(url, token, query, variables or {})
    response.raise_for_status()
    return response.json()
