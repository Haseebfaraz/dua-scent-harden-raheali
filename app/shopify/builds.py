"""Save Build orchestration -- port of app/services/fragranceBuild.server.js's
createShopifyBuildProduct (first-time creation) and app/routes/api.save-build.jsx's action
(re-price/create-variant on an already-created product). Two distinct entry points, exactly as
the reference app keeps them: this module never calls the first for a recommendation that already
has a shopifyProductId, and never calls the second for one that doesn't -- the caller (the ported
preview route) decides which one applies, same as the Node preview action does today.
"""

import json
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.fragrance_build import compute_default_ratios, compute_note_position_buckets, compute_price_per_5ml_by_position
from app.shopify.metafields import build_customer_identity_metafields, build_internal_components_metafield, build_note_composition_metafield
from app.shopify.products import (
    BOTTLE_IMAGE_URL,
    attach_product_media,
    create_product,
    create_variant,
    get_default_variant_id,
    get_product_for_pricing,
    rename_product,
    set_inventory_item_untracked,
    set_variant_price,
)
from app.shopify.publishing import publish_to_all_channels

BOTTLE_ML = 34
POSITION_LABELS = {"top": "Top Note", "middle": "Middle Note", "base": "Base Note"}
OPTION_NAME_TO_POSITION = {v: k for k, v in POSITION_LABELS.items()}

# Two ratios this close together are treated as "the same build" -- imprecise dragging easily
# lands a pixel or two off a previous attempt; without this every tiny wobble would mint its own
# near-duplicate variant instead of reusing the one already close enough.
MATCH_TOLERANCE_PCT = 3

_RATIO_SUFFIX_PATTERN = re.compile(r" \(\d+%\)$")
_RATIO_EXTRACT_PATTERN = re.compile(r"\((\d+)%\)$")


def _strip_ratio_suffix(value: str) -> str:
    return _RATIO_SUFFIX_PATTERN.sub("", value)


def _with_ratio_suffix(base_value: str, pct: float) -> str:
    return f"{_strip_ratio_suffix(base_value)} ({round(pct)}%)"


def _extract_ratio_percent(option_value: str) -> int | None:
    match = _RATIO_EXTRACT_PATTERN.search(option_value)
    return int(match.group(1)) if match else None


class InvalidRatios(Exception):
    pass


async def create_shopify_build_product(
    session: AsyncSession, shop: str, *, recommendation, custom_name: str, ratios: dict[str, float],
    customer_name: str | None, customer_email: str | None,
) -> dict[str, Any]:
    """First-time Shopify product creation for a confirmed recommendation. Never called if
    recommendation.shopifyProductId is already set."""
    pct_sum = ratios["top"] + ratios["middle"] + ratios["base"]
    if pct_sum != 100:
        raise InvalidRatios(f"Top/Middle/Base ratios must sum to 100 (got {pct_sum}).")

    internal_products = recommendation.productsJson if isinstance(recommendation.productsJson, list) else []
    customer_likes = (recommendation.customerProfileJson or {}).get("likes") or []
    buckets = compute_note_position_buckets(internal_products, customer_likes)

    ratios_by_product = recommendation.ratiosJson if isinstance(recommendation.ratiosJson, list) else []
    price_per_5ml_by_position = await compute_price_per_5ml_by_position(session, internal_products, ratios_by_product)

    layers = [
        {
            "position": position,
            "notes": buckets[position],
            "quantityMl": round(((ratios[position] / 100) * BOTTLE_ML) * 10) / 10,
            "pricePer5ml": price_per_5ml_by_position[position],
        }
        for position in ("top", "middle", "base")
    ]
    total_price = sum((layer["pricePer5ml"] / 5) * layer["quantityMl"] for layer in layers)
    price_string = f"{total_price:.2f}"

    product_options = [
        {"name": POSITION_LABELS[layer["position"]], "values": [{"name": f"{', '.join(layer['notes'])} ({round(ratios[layer['position']])}%)"}]}
        for layer in layers
    ]

    full_description = (
        "<p>A bespoke fragrance blend, crafted just for you from real, hand-selected DUA notes — your own signature scent, not a stock formula.</p>"
        "<p><strong>Longevity:</strong> A rich, parfum-concentration blend crafted for long-lasting wear.</p>"
        "<p><strong>Quality:</strong> Lab certified, phthalate &amp; paraben free.</p>"
    )

    metafields = [
        build_note_composition_metafield(recommendation.id, recommendation.combinationType, layers),
        build_internal_components_metafield(internal_products),
        *build_customer_identity_metafields(customer_name, customer_email),
    ]

    product = await create_product(
        session, shop, title=custom_name, description_html=full_description, vendor="The Dua Brand",
        template_suffix="custom-scent", product_options=product_options, metafields=metafields,
    )
    product_id = product["id"]

    try:
        await attach_product_media(session, shop, product_id, BOTTLE_IMAGE_URL, custom_name)
    except Exception:
        pass  # best-effort, matches the JS original's caught-and-logged failure

    try:
        await publish_to_all_channels(session, shop, product_id)
    except Exception:
        pass  # best-effort, matches the JS original's caught-and-logged failure

    default_variant_id = await get_default_variant_id(session, shop, product_id)
    if default_variant_id:
        await set_variant_price(session, shop, product_id, default_variant_id, price_string)

    clean_shop_domain = shop.removeprefix("https://").removeprefix("http://")
    product_url = f"https://{clean_shop_domain}/products/{product['handle']}"

    return {"productId": product_id, "variantId": default_variant_id, "price": float(price_string), "productUrl": product_url}


class ProductPricingNotFound(Exception):
    pass


class InvalidComputedPrice(Exception):
    pass


async def reprice_existing_build(
    session: AsyncSession, shop: str, *, product_id: str, ratios: dict[str, float], name: str | None = None
) -> dict[str, Any]:
    """Port of api.save-build.jsx's action -- re-price/create-a-variant for a NEW ratio on an
    EXISTING product. Deliberately never mutates a shared variant's price in place: Shopify carts
    always display a variant's *current* price, so two different saved ratios sharing one variant
    would silently change the price of an item already sitting in someone else's cart. Every
    distinct ratio (outside MATCH_TOLERANCE_PCT of an existing one) gets its own variant instead.
    """
    await rename_product(session, shop, product_id, name)

    product = await get_product_for_pricing(session, shop, product_id)
    metafield = (product or {}).get("metafield")
    metafield_value = metafield.get("value") if metafield else None
    variant_edges = (product or {}).get("variants", {}).get("edges", [])
    if not product or not metafield_value or not variant_edges:
        raise ProductPricingNotFound("Could not find product or its note composition.")

    layers = json.loads(metafield_value)["layers"]

    by_position: dict[str, dict[str, float]] = {}
    total_ml = 0.0
    for layer in layers:
        ml = layer.get("quantityMl") or 0
        cost = (ml / 5) * (layer.get("pricePer5ml") or 0)
        total_ml += ml
        bucket = by_position.setdefault(layer["position"], {"ml": 0.0, "cost": 0.0})
        bucket["ml"] += ml
        bucket["cost"] += cost

    new_price = 0.0
    for position, pct in ratios.items():
        bucket = by_position.get(position)
        if not bucket or bucket["ml"] == 0:
            continue
        rate = bucket["cost"] / bucket["ml"]
        new_ml = (pct / 100) * total_ml
        new_price += rate * new_ml

    if not (new_price > 0):
        raise InvalidComputedPrice("Computed price was invalid.")

    price_string = f"{new_price:.2f}"

    original_percents = {position: (bucket["ml"] / total_ml) * 100 for position, bucket in by_position.items() if total_ml}

    closest_edge = None
    closest_distance = float("inf")
    for edge in variant_edges:
        distance = 0.0
        for opt in edge["node"]["selectedOptions"]:
            position = OPTION_NAME_TO_POSITION.get(opt["name"])
            if not position or position not in ratios:
                continue
            variant_pct = _extract_ratio_percent(opt["value"])
            if variant_pct is None:
                variant_pct = original_percents.get(position, 0)
            distance = max(distance, abs(variant_pct - ratios[position]))
        if distance < closest_distance:
            closest_distance = distance
            closest_edge = edge

    if closest_edge is not None and closest_distance <= MATCH_TOLERANCE_PCT:
        inventory_item = closest_edge["node"].get("inventoryItem") or {}
        if inventory_item.get("tracked"):
            await set_inventory_item_untracked(session, shop, inventory_item.get("id"))
        return {"price": closest_edge["node"]["price"], "variantId": closest_edge["node"]["id"], "created": False}

    reference_variant = variant_edges[0]["node"]
    target_option_values = [
        {
            "optionName": opt["name"],
            "name": _with_ratio_suffix(opt["value"], ratios[OPTION_NAME_TO_POSITION[opt["name"]]]) if opt["name"] in OPTION_NAME_TO_POSITION else opt["value"],
        }
        for opt in reference_variant["selectedOptions"]
    ]

    new_variant = await create_variant(session, shop, product_id, price_string, target_option_values)
    return {"price": new_variant["price"], "variantId": new_variant["id"], "created": True}
