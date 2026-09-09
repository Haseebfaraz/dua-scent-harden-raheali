"""Port of Node's api.save-build.jsx -- called directly (cross-origin fetch, not via App Proxy)
by the live custom-scent-product.liquid theme section every time a customer drags a Top/Middle/
Base note slider on an ALREADY-CREATED product and clicks Save Build or Add to Cart. Deliberately
separate from app/api/preview.py's App-Proxy-verified /apps/scent-library/fragrance-preview route:
this one has no recommendationId or App Proxy signature to check, only a bare productId + ratios,
exactly like the Node original -- reuses reprice_existing_build, the same function preview.py
already calls for this exact case.

The Node app that used to serve this same URL had gone stale (an offline Session-table token
issued to a different app's install, since confirmed HTTP 401 -- see app/shopify/admin_auth.py's
docstring). This backend already authenticates via Shopify's client credentials grant instead, so
it doesn't share that failure mode.
"""

import json
import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.db.session import get_session
from app.shopify.admin_client import ShopNotAuthenticated
from app.shopify.builds import InvalidComputedPrice, ProductPricingNotFound, reprice_existing_build

logger = logging.getLogger(__name__)

router = APIRouter()

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type, ngrok-skip-browser-warning",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
}

_NOT_CONNECTED_MESSAGE = "This store isn't connected to Shopify for building products right now — an admin needs to reconnect the app before builds can be saved."


class SaveBuildRequest(BaseModel):
    productId: str
    ratios: dict[str, float]
    name: str | None = None


def _shop_from_origin(request: Request) -> str:
    origin = request.headers.get("origin") or ""
    return origin.removeprefix("https://").removeprefix("http://").split("/")[0] or "test-3d-products.myshopify.com"


@router.options("/api/save-build")
async def save_build_preflight() -> JSONResponse:
    return JSONResponse(content=None, status_code=204, headers=_CORS_HEADERS)


@router.post("/api/save-build")
async def save_build(body: SaveBuildRequest, request: Request, session: AsyncSession = Depends(get_session)) -> JSONResponse:
    shop = _shop_from_origin(request)

    try:
        result = await reprice_existing_build(session, shop, product_id=body.productId, ratios=body.ratios, name=body.name)
        return JSONResponse(content=result, status_code=200, headers=_CORS_HEADERS)
    except ShopNotAuthenticated:
        logger.error("SAVE_BUILD_FAILED %s", json.dumps({"shop": shop, "productId": body.productId, "reason": "not_authenticated"}))
        return JSONResponse(content={"error": _NOT_CONNECTED_MESSAGE}, status_code=401, headers=_CORS_HEADERS)
    except ProductPricingNotFound as err:
        return JSONResponse(content={"error": str(err)}, status_code=404, headers=_CORS_HEADERS)
    except InvalidComputedPrice as err:
        return JSONResponse(content={"error": str(err)}, status_code=400, headers=_CORS_HEADERS)
    except httpx.HTTPStatusError as err:
        # The exact failure this endpoint replaces the old Node route to avoid: a token Shopify
        # itself rejects (401/403), previously swallowed into a generic, undiagnosable message.
        status = err.response.status_code
        logger.error("SAVE_BUILD_FAILED %s", json.dumps({"shop": shop, "productId": body.productId, "reason": "shopify_http_error", "status": status}))
        if status in (401, 403):
            return JSONResponse(content={"error": _NOT_CONNECTED_MESSAGE}, status_code=401, headers=_CORS_HEADERS)
        return JSONResponse(content={"error": "Shopify couldn't process this build right now — please try again shortly."}, status_code=502, headers=_CORS_HEADERS)
    except Exception as err:
        logger.error("SAVE_BUILD_FAILED %s", json.dumps({"shop": shop, "productId": body.productId, "reason": "unexpected", "errorType": type(err).__name__}))
        return JSONResponse(content={"error": "Failed to save build."}, status_code=500, headers=_CORS_HEADERS)
