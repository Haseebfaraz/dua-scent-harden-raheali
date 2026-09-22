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


class ShopifyTransportError(Exception):
    """Phase 7: the Admin API answered, but not with a usable GraphQL result: top-level GraphQL
    `errors` (throttling, an invalid query, a version error), a body that is not JSON, or a body
    without `data`. Callers treat it exactly like a rejected mutation: nothing may proceed as if
    the operation succeeded. The message never carries the raw payload."""

    def __init__(self, reason: str, *, throttled: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.throttled = throttled


class ShopifyApiVersionMismatch(ShopifyTransportError):
    """Shopify served a DIFFERENT API version from the one requested (the version was unsupported
    and Shopify "fell forward"). A mutation may already have executed under that other version,
    so this is raised AFTER the request and callers handle it as an ambiguous outcome, never as
    success and never with an automatic retry."""

    def __init__(self, requested: str, served: str):
        super().__init__("api_version_mismatch")
        self.requested = requested
        self.served = served


_VERSION_HEADER = "X-Shopify-API-Version"


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

    requested_version = settings.shopify_api_version
    url = f"https://{shop}/admin/api/{requested_version}/graphql.json"
    response = await _post(url, token, query, variables or {})
    response.raise_for_status()
    # Phase 7 (F10): Shopify answers an unsupported version with the oldest supported one and
    # says so in this header. A different served version is never treated as validation of the
    # requested one; it is an error the caller must handle (ambiguous if a mutation was sent).
    served_version = response.headers.get(_VERSION_HEADER)
    if served_version and served_version != requested_version:
        raise ShopifyApiVersionMismatch(requested_version, served_version)
    try:
        payload = response.json()
    except ValueError:
        raise ShopifyTransportError("malformed_json") from None
    if not isinstance(payload, dict):
        raise ShopifyTransportError("malformed_json")
    errors = payload.get("errors")
    if errors:
        # Top-level errors mean the operation did not run as written (or was throttled). Only a
        # safe reason code leaves this function; the raw messages stay out of logs and customers.
        throttled = any(isinstance(e, dict) and ((e.get("extensions") or {}).get("code") == "THROTTLED") for e in errors)
        raise ShopifyTransportError("throttled" if throttled else "graphql_errors", throttled=throttled)
    if "data" not in payload:
        raise ShopifyTransportError("missing_data")
    return payload
