"""Shopify's two HMAC schemes, ported by hand -- both are small, public, well-documented
algorithms; no official Shopify Python SDK is needed for either.

Webhook HMAC: base64(HMAC-SHA256(raw request body, client secret)) compared against the
X-Shopify-Hmac-Sha256 header. App Proxy signature: HMAC-SHA256 (hex) over the sorted, unescaped
query string (excluding `signature` itself) with no separators between "key=value" pairs.

Ref (Node reference app): @shopify/shopify-app-react-router's authenticate.webhook /
authenticate.public.appProxy do exactly this internally.
"""

import base64
import hashlib
import hmac as _hmac


def verify_webhook_hmac(raw_body: bytes, header_value: str | None, secret: str) -> bool:
    if not header_value or not secret:
        return False
    digest = base64.b64encode(_hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()).decode()
    return _hmac.compare_digest(digest, header_value)


def verify_app_proxy_signature(query_params: dict[str, str], secret: str) -> bool:
    # ponytail: assumes one value per key (true for every param App Proxy actually sends to our
    # routes -- shop/timestamp/path_prefix/logged_in_customer_id/recommendationId/signature).
    # Shopify's spec joins repeated keys with commas before hashing; add that if a route ever
    # needs a genuinely multi-valued query param.
    signature = query_params.get("signature")
    if not signature or not secret:
        return False
    message = "".join(f"{k}={v}" for k, v in sorted((k, v) for k, v in query_params.items() if k != "signature"))
    digest = _hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return _hmac.compare_digest(digest, signature)
