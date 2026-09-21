"""Direct (non-App-Proxy) re-price endpoint the storefront theme's note sliders call on an
ALREADY-CREATED build product.

Phase 1 (security, F1 / F2 / N1 / N3) replaced the previous contract entirely. The old request
(`{productId, ratios, name}` with the shop taken from the browser's Origin header) let anyone on
the internet pick the Shopify host that received this app's client credentials, rename or
re-variant any product in the store by GID, and price a variant from an incomplete ratio set.
None of those inputs are accepted as authority any more:

  * the shop is the configured trusted shop, full stop (app/shopify/trusted_shop.py);
  * the caller presents a `recommendationId` AND the build capability token (`buildToken`)
    minted for it when the preview opened (app/services/build_capability.py) -- knowing an id
    alone is not authorization;
  * the Shopify product id comes from the recommendation row, and the product is verified to be
    that recommendation's own custom-scent build before any write (app/shopify/builds.py);
  * ratios and name go through the shared validator; price is computed server-side.

The full contract, including the required theme change, is in
docs/SHOPIFY_BUILD_SECURITY_CONTRACT.md. Requests in the old shape get a 400 with
`code: "build_contract_upgraded"` -- there is deliberately no insecure compatibility path.

CORS: this is a privileged mutation endpoint, so `Access-Control-Allow-Origin: *` is gone. Only
the trusted storefront origins (https://<trusted shop> plus ALLOWED_ORIGINS) are reflected, with
`Vary: Origin`; any other browser origin is refused. CORS is not authorization -- every request,
with or without an Origin header, still needs a valid capability.
"""

import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import get_session
from app.services.build_capability import BuildNotAuthorized, authorize_build_token
from app.services.build_commerce import BuildOperationInProgress, BuildPendingReview, execute_build_commerce
from app.services.commerce_inventory import InventoryNotVerified, commerce_failure
from app.services.recommendation_confirmation import get_recommendation
from app.shopify.admin_client import ShopNotAuthenticated
from app.shopify.build_input import InvalidCustomName, InvalidRatios, validate_custom_name, validate_ratios
from app.shopify.builds import BuildProductNotSaved, BuildWriteAmbiguous, InvalidComputedPrice, ProductPricingNotFound
from app.shopify.trusted_shop import TrustedShopNotConfigured, UntrustedShopError, trusted_shop

logger = logging.getLogger(__name__)

router = APIRouter()

_NOT_CONNECTED_MESSAGE = "This store isn't connected to Shopify for building products right now — an admin needs to reconnect the app before builds can be saved."
_NOT_AUTHORIZED_MESSAGE = "This fragrance build link is not valid or has expired. Please reopen your fragrance preview from the chat."
_CONTRACT_UPGRADED_MESSAGE = "This page needs to be refreshed to save builds. Please reopen your fragrance preview from the chat."


class SaveBuildRequest(BaseModel):
    # Unknown fields (including the retired `productId`) are rejected so the old contract cannot
    # silently half-work.
    model_config = ConfigDict(extra="forbid")

    recommendationId: str
    buildToken: str
    # Validated by app/shopify/build_input.py, not by pydantic's lenient float coercion.
    ratios: Any
    name: Any = None


def _allowed_origins() -> set[str]:
    origins = set(settings.allowed_origins_list)
    try:
        origins.add(f"https://{trusted_shop()}")
    except UntrustedShopError:
        pass
    return origins


def _cors_headers_for(request: Request) -> dict[str, str] | None:
    """Exact-match CORS headers for a trusted storefront origin; None for any other origin.
    Requests with no Origin header (non-browser callers) get no CORS headers and are not
    refused on that basis -- authorization is enforced separately."""
    origin = request.headers.get("origin")
    if origin is None:
        return {}
    if origin not in _allowed_origins():
        return None
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Vary": "Origin",
    }


def _json(content: dict, status_code: int, cors: dict[str, str]) -> JSONResponse:
    return JSONResponse(content=content, status_code=status_code, headers=cors)


@router.options("/api/save-build")
async def save_build_preflight(request: Request) -> Response:
    cors = _cors_headers_for(request)
    if cors is None:
        return Response(status_code=403)
    return Response(status_code=204, headers={**cors, "Access-Control-Max-Age": "600"})


@router.post("/api/save-build")
async def save_build(request: Request, session: AsyncSession = Depends(get_session)) -> JSONResponse:
    cors = _cors_headers_for(request)
    if cors is None:
        # Unknown browser origin. Not a security boundary by itself, but there is no legitimate
        # cross-origin caller other than the trusted storefront.
        return JSONResponse(content={"error": "Origin not allowed.", "code": "origin_not_allowed"}, status_code=403)

    # ---- parse (the retired {productId, ...} shape is refused explicitly) ----
    try:
        raw = await request.json()
    except ValueError:
        return _json({"error": "Invalid request.", "code": "invalid_json"}, 400, cors)
    if isinstance(raw, dict) and "productId" in raw and "buildToken" not in raw:
        return _json({"error": _CONTRACT_UPGRADED_MESSAGE, "code": "build_contract_upgraded"}, 400, cors)
    try:
        body = SaveBuildRequest.model_validate(raw)
    except Exception:
        return _json({"error": "Invalid request.", "code": "invalid_request"}, 400, cors)

    # ---- VALIDATE INPUT (before any lookup) ----
    try:
        ratios = validate_ratios(body.ratios)
        name = validate_custom_name(body.name)
    except InvalidRatios as err:
        return _json({"error": str(err), "code": "invalid_ratios"}, 400, cors)
    except InvalidCustomName as err:
        return _json({"error": str(err), "code": "invalid_name"}, 400, cors)

    # ---- AUTHENTICATE (trusted shop) ----
    try:
        shop = trusted_shop()
    except TrustedShopNotConfigured:
        logger.error("SAVE_BUILD_FAILED %s", json.dumps({"reason": "trusted_shop_not_configured"}))
        return _json({"error": _NOT_CONNECTED_MESSAGE}, 503, cors)

    # ---- AUTHORIZE (capability binds the caller to exactly this recommendation) ----
    try:
        await authorize_build_token(session, token=body.buildToken, recommendation_id=body.recommendationId)
    except BuildNotAuthorized:
        logger.info("SAVE_BUILD_REJECTED %s", json.dumps({"recommendationId": body.recommendationId, "reason": "build_not_authorized"}))
        return _json({"error": _NOT_AUTHORIZED_MESSAGE, "code": "build_not_authorized"}, 403, cors)

    recommendation = await get_recommendation(session, body.recommendationId)
    if recommendation is None:
        return _json({"error": _NOT_AUTHORIZED_MESSAGE, "code": "build_not_authorized"}, 403, cors)

    # ---- VALIDATE PRODUCT, COMPUTE, WRITE (all inside reprice_existing_build, in that order) ----
    # Phase 5 (F9): same orchestration as the preview actions. This endpoint never creates a
    # product (allow_create=False); inventory is verified inside the write layer before any write
    # and before a variant id is handed back.
    try:
        outcome = await execute_build_commerce(session, shop, recommendation_id=body.recommendationId, ratios=ratios, name=name, allow_create=False, want_product_url=False, record_saved=False)
        return _json({"price": outcome["price"], "variantId": outcome["variantId"], "created": outcome["created"]}, 200, cors)
    except InventoryNotVerified as err:
        logger.info("SAVE_BUILD_REJECTED %s", json.dumps({"recommendationId": body.recommendationId, "reason": "inventory", "state": err.state.value}))
        status, payload = commerce_failure(err.state)
        return _json(payload, status, cors)
    except BuildOperationInProgress:
        status, payload = commerce_failure("build_in_progress")
        return _json(payload, status, cors)
    except (BuildPendingReview, BuildWriteAmbiguous):
        status, payload = commerce_failure("build_pending_review")
        return _json(payload, status, cors)
    except BuildProductNotSaved as err:
        return _json({"error": str(err), "code": "build_not_saved"}, 409, cors)
    except UntrustedShopError:
        logger.error("SAVE_BUILD_FAILED %s", json.dumps({"recommendationId": body.recommendationId, "reason": "untrusted_shop"}))
        return _json({"error": _NOT_CONNECTED_MESSAGE}, 503, cors)
    except ShopNotAuthenticated:
        logger.error("SAVE_BUILD_FAILED %s", json.dumps({"recommendationId": body.recommendationId, "reason": "not_authenticated"}))
        return _json({"error": _NOT_CONNECTED_MESSAGE}, 401, cors)
    except ProductPricingNotFound as err:
        logger.info("SAVE_BUILD_REJECTED %s", json.dumps({"recommendationId": body.recommendationId, "reason": type(err).__name__}))
        return _json({"error": str(err), "code": "build_product_invalid"}, 404, cors)
    except InvalidComputedPrice as err:
        return _json({"error": str(err), "code": "invalid_price"}, 400, cors)
    except httpx.HTTPStatusError as err:
        status = err.response.status_code
        logger.error("SAVE_BUILD_FAILED %s", json.dumps({"recommendationId": body.recommendationId, "reason": "shopify_http_error", "status": status}))
        if status in (401, 403):
            return _json({"error": _NOT_CONNECTED_MESSAGE}, 401, cors)
        return _json({"error": "Shopify couldn't process this build right now — please try again shortly."}, 502, cors)
    except Exception as err:
        logger.error("SAVE_BUILD_FAILED %s", json.dumps({"recommendationId": body.recommendationId, "reason": "unexpected", "errorType": type(err).__name__}))
        return _json({"error": "Failed to save build."}, 500, cors)
