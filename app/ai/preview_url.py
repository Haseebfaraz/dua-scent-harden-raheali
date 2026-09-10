"""Port of app/utils/previewUrl.server.js -- every place that builds a preview_ready event's
previewUrl goes through this one function.

Unlike the JS version, Python has no access to the Shopify Session table (Node/Prisma-owned) --
shop_domain must be supplied by the caller (the Node adapter passes it through the internal
chat request, since it already resolves it via resolveShopDomain()).
"""

from urllib.parse import urlencode

from app.services.build_capability import BUILD_TOKEN_QUERY_PARAM


def build_preview_url(shop_domain: str, recommendation_id: str, build_token: str | None = None) -> str:
    """Phase 1 (security, N3): the preview URL carries the build capability token (`bt`) that
    authorizes reading the preview and mutating the build. Shopify's App Proxy forwards and signs
    every query parameter, so the token reaches app/api/preview.py inside the verified query."""
    params = {"recommendationId": recommendation_id}
    if build_token:
        params[BUILD_TOKEN_QUERY_PARAM] = build_token
    return f"https://{shop_domain}/apps/scent-library/fragrance-preview?{urlencode(params)}"
