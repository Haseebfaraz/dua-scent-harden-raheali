"""FastAPI dependency for App Proxy routes -- port of authenticate.public.appProxy's signature
check. A request that didn't genuinely come through Shopify's proxy never reaches the handler,
same guarantee the Node reference app relies on for apps.scent-library.fragrance-preview.jsx.
"""

from fastapi import HTTPException, Request

from app.config import settings
from app.shopify.hmac import verify_app_proxy_signature


def verified_shop(request: Request) -> str:
    params = dict(request.query_params)
    if not verify_app_proxy_signature(params, settings.shopify_api_secret):
        raise HTTPException(status_code=400, detail="invalid app proxy signature")
    shop = params.get("shop")
    if not shop:
        raise HTTPException(status_code=400, detail="missing shop")
    return shop
