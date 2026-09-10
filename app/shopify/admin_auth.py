"""Server-to-server Admin API auth for DUA Scent AI via Shopify's client credentials grant --
the correct auth model here: this is a Shopify-managed (CLI-deployed, `embedded = true`) app
installed on a store within its own Partner organization, not a traditional public app that ran
a browser-redirect OAuth install. Client credentials grant lets an app already installed on a
shop in its own org request/refresh an Admin access token directly, server-to-server, with no
redirect and no callback route -- which is exactly what this backend needs, since Python has no
OAuth begin/callback implementation at all.

Falls back to the stored offline Session-table token (see app/shopify/sessions.py) only when
SHOPIFY_API_KEY/SHOPIFY_API_SECRET aren't configured -- never prefers a Session row over a fresh
client-credentials attempt, since that stored token was confirmed live to be invalid for this app
(HTTP 401, and its scope doesn't match this app's configured scopes -- almost certainly issued to
a different app's install, not this one).

Never logs a token, secret, or Authorization header -- only shop, method, and boolean/status
outcomes.
"""

import json
import logging
import time

import httpx

from app.config import settings
from app.shopify.trusted_shop import require_trusted_shop

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 15.0
# Refresh a bit before Shopify's own expiry rather than right at the edge, so an in-flight
# request never gets caught using a token that expires mid-call.
_EXPIRY_SAFETY_MARGIN_SECONDS = 120
_MIN_CACHE_SECONDS = 30

# shop -> (access_token, expires_at_monotonic). Process-local; a cold start or a second worker
# process just means one extra grant request, never an incorrect one -- this is a performance
# cache, not a correctness dependency.
_token_cache: dict[str, tuple[str, float]] = {}


async def _request_client_credentials_token(shop: str) -> dict | None:
    # Phase 1 (F1): the client secret is about to be placed in a request body -- the destination
    # MUST be the configured trusted shop. This raises (no HTTP request is ever built) for any
    # other value, regardless of what the caller already checked.
    shop = require_trusted_shop(shop)

    if not settings.shopify_api_key or not settings.shopify_api_secret:
        return None

    url = f"https://{shop}/admin/oauth/access_token"
    payload = {
        "client_id": settings.shopify_api_key,
        "client_secret": settings.shopify_api_secret,
        "grant_type": "client_credentials",
    }
    try:
        # follow_redirects=False is httpx's default; stated explicitly because this request
        # carries credentials and must never be replayed to a redirect target.
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await client.post(url, json=payload)
    except httpx.HTTPError as err:
        logger.error("SHOPIFY_ADMIN_AUTH_FAILED %s", json.dumps({"shop": shop, "method": "client_credentials", "reason": "network_error", "errorType": type(err).__name__}))
        return None

    if response.status_code != 200:
        logger.error("SHOPIFY_ADMIN_AUTH_FAILED %s", json.dumps({"shop": shop, "method": "client_credentials", "reason": "http_error", "status": response.status_code}))
        return None

    body = response.json()
    if not body.get("access_token"):
        logger.error("SHOPIFY_ADMIN_AUTH_FAILED %s", json.dumps({"shop": shop, "method": "client_credentials", "reason": "missing_access_token_in_response"}))
        return None

    logger.info("SHOPIFY_ADMIN_AUTH_OK %s", json.dumps({"shop": shop, "method": "client_credentials", "expiresInSeconds": body.get("expires_in")}))
    return body


async def get_admin_access_token(session, shop: str) -> tuple[str | None, str]:
    """Returns (token, source). source is one of:
    "client_credentials_cached", "client_credentials", "session_table", or "none".
    Callers should treat "none" as ShopNotAuthenticated -- there is no usable credential.

    Raises UntrustedShopError before touching the cache, the network, or the database when
    `shop` is not the configured trusted shop (Phase 1, F1).
    """
    shop = require_trusted_shop(shop)
    now = time.monotonic()
    cached = _token_cache.get(shop)
    if cached and cached[1] > now:
        logger.info("SHOPIFY_AUTH_SOURCE %s", json.dumps({"shop": shop, "source": "client_credentials_cached"}))
        return cached[0], "client_credentials_cached"

    token_data = await _request_client_credentials_token(shop)
    if token_data:
        expires_in = token_data.get("expires_in") or 3600
        ttl = max(expires_in - _EXPIRY_SAFETY_MARGIN_SECONDS, _MIN_CACHE_SECONDS)
        _token_cache[shop] = (token_data["access_token"], now + ttl)
        logger.info("SHOPIFY_AUTH_SOURCE %s", json.dumps({"shop": shop, "source": "client_credentials"}))
        return token_data["access_token"], "client_credentials"

    # Client credentials not configured or not available for this shop/app -- fall back to
    # whatever offline Session row exists, but never treat it as preferred.
    from app.shopify.sessions import get_offline_access_token

    session_token = await get_offline_access_token(session, shop)
    if session_token:
        logger.info("SHOPIFY_AUTH_SOURCE %s", json.dumps({"shop": shop, "source": "session_table"}))
        return session_token, "session_table"

    logger.info("SHOPIFY_AUTH_SOURCE %s", json.dumps({"shop": shop, "source": "none"}))
    return None, "none"
