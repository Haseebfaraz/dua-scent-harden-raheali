"""Port of apps.scent-library.fragrance-preview.jsx's loader (GET) and action (POST). Same App
Proxy path, same recommendation lookup, same recreate/save_build/add_to_cart behavior -- only the
rendering technology changed (Jinja2 + vanilla JS instead of React), per the explicit decision to
migrate, not redesign, this page.
"""

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.services.customer_profile import get_customer_profile, save_customer_profile_field
from app.services.fragrance_build import compute_default_ratios, compute_note_position_buckets, compute_price_per_5ml_by_position
from app.services.recommendation_confirmation import get_recommendation, mark_recommendation_draft, mark_recommendation_saved
from app.shopify.app_proxy import verified_shop
from app.shopify.builds import InvalidComputedPrice, InvalidRatios, ProductPricingNotFound, create_shopify_build_product, reprice_existing_build
from app.shopify.products import get_product_handle

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# Shopify's numeric REST-style ID from a GraphQL GID (e.g. "gid://shopify/ProductVariant/123" ->
# "123") -- needed for the cart permalink URL format (/cart/{variantId}:{quantity}).
_NUMERIC_ID_PATTERN = re.compile(r"(\d+)$")


def _numeric_id_from_gid(gid: str | None) -> str | None:
    match = _NUMERIC_ID_PATTERN.search(str(gid or ""))
    return match.group(1) if match else None


def _safe_json_for_script_tag(data: dict[str, Any]) -> str:
    # Prevents a "</script>" inside any string field (a product title, a note) from prematurely
    # closing the <script> tag this gets embedded in -- a standard, safe JSON-in-HTML technique.
    return json.dumps(data).replace("</", "<\\/")


@router.get("/apps/scent-library/fragrance-preview", response_class=HTMLResponse)
async def preview_page(request: Request, shop: str = Depends(verified_shop), session: AsyncSession = Depends(get_session)):
    recommendation_id = request.query_params.get("recommendationId")
    if not recommendation_id:
        raise HTTPException(status_code=400, detail="recommendationId is required")

    recommendation = await get_recommendation(session, recommendation_id)
    if not recommendation:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    internal_products = recommendation.productsJson if isinstance(recommendation.productsJson, list) else []
    customer_likes = (recommendation.customerProfileJson or {}).get("likes") or []
    buckets = compute_note_position_buckets(internal_products, customer_likes)
    ratios = recommendation.draftRatiosJson or compute_default_ratios(buckets)
    customer_facing_name = (recommendation.customerFacingJson or {}).get("customerFacingName") or "Custom Blend"

    ratios_by_product = recommendation.ratiosJson if isinstance(recommendation.ratiosJson, list) else []
    price_per_5ml_by_position = await compute_price_per_5ml_by_position(session, internal_products, ratios_by_product)

    profile_pills = list(dict.fromkeys([*buckets["top"], *buckets["middle"], *buckets["base"]]))[:8]

    data = {
        "recommendationId": recommendation_id,
        "name": recommendation.draftName or customer_facing_name,
        "buckets": buckets,
        "ratios": ratios,
        "buildStatus": recommendation.buildStatus,
        "shopifyProductId": recommendation.shopifyProductId,
        "pricePer5mlByPosition": price_per_5ml_by_position,
        "profilePills": profile_pills,
        "productsUsed": [{"title": p.get("title"), "contribution": p.get("contribution")} for p in internal_products],
    }

    return templates.TemplateResponse(
        request, "fragrance_preview.html",
        {"data": data, "data_json": _safe_json_for_script_tag(data)},
    )


class PreviewAction(BaseModel):
    intent: str
    recommendationId: str
    name: str | None = None
    ratios: dict[str, float] | None = None


@router.post("/apps/scent-library/fragrance-preview")
async def preview_action(body: PreviewAction, shop: str = Depends(verified_shop), session: AsyncSession = Depends(get_session)) -> dict:
    recommendation = await get_recommendation(session, body.recommendationId)
    if not recommendation:
        return {"error": "Recommendation not found."}

    if body.intent == "recreate":
        await mark_recommendation_draft(session, body.recommendationId, name=body.name, ratios=body.ratios)
        await save_customer_profile_field(session, recommendation.conversationId, "pendingRecreateRecommendationId", body.recommendationId)
        return {"status": "recreate", "redirectUrl": f"https://{shop}/"}

    if body.intent in ("save_build", "add_to_cart"):
        await mark_recommendation_draft(session, body.recommendationId, name=body.name, ratios=body.ratios)

        shopify_product_id = recommendation.shopifyProductId
        shopify_variant_id = recommendation.shopifyVariantId
        product_url = None

        try:
            if not shopify_product_id:
                identity_profile = await get_customer_profile(session, recommendation.conversationId)
                result = await create_shopify_build_product(
                    session, shop, recommendation=recommendation,
                    custom_name=body.name or (recommendation.customerFacingJson or {}).get("customerFacingName") or "Custom Blend",
                    ratios=body.ratios, customer_name=identity_profile.get("name"), customer_email=identity_profile.get("email"),
                )
                shopify_product_id = result["productId"]
                shopify_variant_id = result["variantId"]
                product_url = result["productUrl"]
            else:
                reprice = await reprice_existing_build(session, shop, product_id=shopify_product_id, ratios=body.ratios, name=body.name)
                shopify_variant_id = reprice["variantId"]
                handle = await get_product_handle(session, shop, shopify_product_id)
                product_url = f"https://{shop}/products/{handle}" if handle else None
        except (InvalidRatios, InvalidComputedPrice, ProductPricingNotFound) as err:
            return {"error": str(err)}
        except Exception:
            return {"error": "Failed to save the build."}

        await mark_recommendation_saved(session, body.recommendationId, shopify_product_id=shopify_product_id, shopify_variant_id=shopify_variant_id)

        if body.intent == "save_build":
            return {"status": "saved", "shopifyProductId": shopify_product_id, "shopifyVariantId": shopify_variant_id, "productUrl": product_url}

        numeric_variant_id = _numeric_id_from_gid(shopify_variant_id)
        return {"status": "added", "shopifyProductId": shopify_product_id, "shopifyVariantId": shopify_variant_id, "cartUrl": f"https://{shop}/cart/{numeric_variant_id}:1"}

    raise HTTPException(status_code=400, detail=f'Unknown intent "{body.intent}".')
