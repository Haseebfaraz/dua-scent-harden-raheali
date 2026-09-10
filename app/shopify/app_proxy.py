"""FastAPI dependency for App Proxy routes -- port of authenticate.public.appProxy's signature
check. A request that didn't genuinely come through Shopify's proxy never reaches the handler,
same guarantee the Node reference app relies on for apps.scent-library.fragrance-preview.jsx.

Phase 1 (security, F1): a valid signature proves Shopify proxied the request for `shop`; it does
not prove `shop` is OUR store (any store that installed a same-secret app could sign). The shop
must additionally equal the configured trusted shop before it is used for anything.
"""

from fastapi import HTTPException, Request

from app.config import settings
from app.shopify.hmac import verify_app_proxy_signature
from app.shopify.trusted_shop import UntrustedShopError, require_trusted_shop


def verified_signed_params(request: Request) -> dict:
    """The App Proxy query parameters, ONLY after the signature verified and the shop matched the
    trusted shop. `shop` is replaced by its canonical form. `logged_in_customer_id`, when present
    and non-empty, is a Shopify-verified customer id (see app/services/customer_identity.py) --
    which still says nothing about which objects that customer owns."""
    params = dict(request.query_params)
    if not verify_app_proxy_signature(params, settings.shopify_api_secret):
        raise HTTPException(status_code=400, detail="invalid app proxy signature")
    shop = params.get("shop")
    if not shop:
        raise HTTPException(status_code=400, detail="missing shop")
    try:
        params["shop"] = require_trusted_shop(shop)
    except UntrustedShopError:
        raise HTTPException(status_code=403, detail="shop not trusted") from None
    return params


def verified_shop(request: Request) -> str:
    return verified_signed_params(request)["shop"]
