"""Port of app/utils/previewUrl.server.js -- every place that builds a preview_ready event's
previewUrl goes through this one function.

Unlike the JS version, Python has no access to the Shopify Session table (Node/Prisma-owned) --
shop_domain must be supplied by the caller (the Node adapter passes it through the internal
chat request, since it already resolves it via resolveShopDomain()).
"""

from urllib.parse import urlencode


def build_preview_url(shop_domain: str, recommendation_id: str) -> str:
    query = urlencode({"recommendationId": recommendation_id})
    return f"https://{shop_domain}/apps/scent-library/fragrance-preview?{query}"
