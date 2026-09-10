"""Save Build orchestration -- port of app/services/fragranceBuild.server.js's
createShopifyBuildProduct (first-time creation) and app/routes/api.save-build.jsx's action
(re-price/create-variant on an already-created product). Two distinct entry points, exactly as
the reference app keeps them: this module never calls the first for a recommendation that already
has a shopifyProductId, and never calls the second for one that doesn't -- the caller decides
which one applies.

Phase 1 (security, F1 / F2 / N1 / N3) changed the trust model of BOTH entry points:

  * the shop is always the configured trusted shop (validated again here and, independently, in
    every credential-bearing Shopify client);
  * the Shopify product id is never accepted from a caller -- it is read from the
    FragranceRecommendation row the caller has already been authorized for (build capability,
    see app/services/build_capability.py);
  * before ANY write, the product Shopify returns is verified to be that recommendation's own
    custom-scent build (id, vendor, template, `custom.note_composition.recommendationId`,
    layers, variants);
  * ratios and the custom name go through the single shared validator
    (app/shopify/build_input.py);
  * the price is a server-side computation from the product's own stored layer data plus the
    validated 100% composition, checked finite and positive;
  * order is READ -> AUTHORIZE -> VALIDATE -> COMPUTE -> WRITE. The rename is the LAST write.
"""

import json
import math
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.fragrance_build import compute_note_position_buckets, compute_price_per_5ml_by_position
from app.shopify.build_input import InvalidCustomName, InvalidRatios, POSITIONS, validate_custom_name, validate_ratios
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
from app.shopify.trusted_shop import require_trusted_shop

BOTTLE_ML = 34
POSITION_LABELS = {"top": "Top Note", "middle": "Middle Note", "base": "Base Note"}
OPTION_NAME_TO_POSITION = {v: k for k, v in POSITION_LABELS.items()}

# Identity every Scent AI build product is created with (see create_shopify_build_product) and
# therefore must still carry before it may be mutated as one.
BUILD_PRODUCT_VENDOR = "The Dua Brand"
BUILD_PRODUCT_TEMPLATE_SUFFIX = "custom-scent"

# Two ratios this close together are treated as "the same build" -- imprecise dragging easily
# lands a pixel or two off a previous attempt; without this every tiny wobble would mint its own
# near-duplicate variant instead of reusing the one already close enough.
MATCH_TOLERANCE_PCT = 3

_RATIO_SUFFIX_PATTERN = re.compile(r" \(\d+%\)$")
_RATIO_EXTRACT_PATTERN = re.compile(r"\((\d+)%\)$")

__all__ = [
    "BuildProductMismatch",
    "BuildProductNotSaved",
    "InvalidComputedPrice",
    "InvalidCustomName",
    "InvalidRatios",
    "ProductPricingNotFound",
    "create_shopify_build_product",
    "reprice_existing_build",
    "verify_build_product",
]


def _strip_ratio_suffix(value: str) -> str:
    return _RATIO_SUFFIX_PATTERN.sub("", value)


def _with_ratio_suffix(base_value: str, pct: float) -> str:
    return f"{_strip_ratio_suffix(base_value)} ({round(pct)}%)"


def _extract_ratio_percent(option_value: str) -> int | None:
    match = _RATIO_EXTRACT_PATTERN.search(option_value)
    return int(match.group(1)) if match else None


class ProductPricingNotFound(Exception):
    """The Shopify product is missing or is not a valid Scent AI build for this recommendation."""


class BuildProductMismatch(ProductPricingNotFound):
    """The product Shopify returned is not the authorized recommendation's own build product."""


class BuildProductNotSaved(Exception):
    """The recommendation has no Shopify product yet -- the preview flow must create it first."""


class InvalidComputedPrice(Exception):
    pass


async def create_shopify_build_product(
    session: AsyncSession, shop: str, *, recommendation, custom_name: str, ratios: dict[str, Any],
    customer_name: str | None, customer_email: str | None,
) -> dict[str, Any]:
    """First-time Shopify product creation for a confirmed recommendation. Never called if
    recommendation.shopifyProductId is already set. The caller must already have authorized the
    recommendation (build capability); this function re-validates every input itself."""
    shop = require_trusted_shop(shop)
    ratios = validate_ratios(ratios)
    custom_name = validate_custom_name(custom_name, required=True)

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
        for position in POSITIONS
    ]
    total_price = sum((layer["pricePer5ml"] / 5) * layer["quantityMl"] for layer in layers)
    price_string = _price_string(total_price)

    product_options = [
        {"name": POSITION_LABELS[layer["position"]], "values": [{"name": f"{', '.join(layer['notes'])} ({ratios[layer['position']]}%)"}]}
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

    # Every check above passed -- only now does the first write happen.
    product = await create_product(
        session, shop, title=custom_name, description_html=full_description, vendor=BUILD_PRODUCT_VENDOR,
        template_suffix=BUILD_PRODUCT_TEMPLATE_SUFFIX, product_options=product_options, metafields=metafields,
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

    product_url = f"https://{shop}/products/{product['handle']}"

    return {"productId": product_id, "variantId": default_variant_id, "price": float(price_string), "productUrl": product_url}


def _price_string(total_price: float) -> str:
    if not isinstance(total_price, (int, float)) or isinstance(total_price, bool) or not math.isfinite(total_price) or not total_price > 0:
        raise InvalidComputedPrice("Computed price was invalid.")
    return f"{total_price:.2f}"


def verify_build_product(product: dict[str, Any] | None, recommendation) -> dict[str, Any]:
    """Prove that `product` (the getProductForPricing response) is the authorized recommendation's
    own custom-scent build. Returns the parsed note_composition. Raises BuildProductMismatch or
    ProductPricingNotFound otherwise. Product existence alone is never enough."""
    if not product:
        raise ProductPricingNotFound("Could not find product or its note composition.")
    expected_product_id = recommendation.shopifyProductId
    if not expected_product_id or product.get("id") != expected_product_id:
        raise BuildProductMismatch("Could not find product or its note composition.")
    if product.get("vendor") != BUILD_PRODUCT_VENDOR or product.get("templateSuffix") != BUILD_PRODUCT_TEMPLATE_SUFFIX:
        raise BuildProductMismatch("Could not find product or its note composition.")

    metafield = product.get("metafield") or {}
    metafield_value = metafield.get("value") if isinstance(metafield, dict) else None
    if not isinstance(metafield_value, str) or not metafield_value:
        raise ProductPricingNotFound("Could not find product or its note composition.")
    try:
        composition = json.loads(metafield_value)
    except ValueError:
        raise BuildProductMismatch("Could not find product or its note composition.") from None
    if not isinstance(composition, dict) or composition.get("recommendationId") != recommendation.id:
        raise BuildProductMismatch("Could not find product or its note composition.")
    layers = composition.get("layers")
    if not isinstance(layers, list) or not layers:
        raise BuildProductMismatch("Could not find product or its note composition.")
    layer_positions = {layer.get("position") for layer in layers if isinstance(layer, dict)}
    if not set(POSITIONS).issubset(layer_positions):
        raise BuildProductMismatch("Could not find product or its note composition.")

    variant_edges = ((product.get("variants") or {}).get("edges")) or []
    if not variant_edges:
        raise ProductPricingNotFound("Could not find product or its note composition.")
    return composition


def _compute_reprice(layers: list[dict], ratios: dict[str, int]) -> tuple[str, dict[str, float], dict[str, dict[str, float]]]:
    by_position: dict[str, dict[str, float]] = {}
    total_ml = 0.0
    for layer in layers:
        ml = layer.get("quantityMl") or 0
        cost = (ml / 5) * (layer.get("pricePer5ml") or 0)
        total_ml += ml
        bucket = by_position.setdefault(layer["position"], {"ml": 0.0, "cost": 0.0})
        bucket["ml"] += ml
        bucket["cost"] += cost

    if not (isinstance(total_ml, (int, float)) and math.isfinite(total_ml) and total_ml > 0):
        raise InvalidComputedPrice("Computed price was invalid.")

    new_price = 0.0
    for position in POSITIONS:
        bucket = by_position.get(position)
        if not bucket or bucket["ml"] == 0:
            continue
        rate = bucket["cost"] / bucket["ml"]
        new_ml = (ratios[position] / 100) * total_ml
        new_price += rate * new_ml

    price_string = _price_string(new_price)
    original_percents = {position: (bucket["ml"] / total_ml) * 100 for position, bucket in by_position.items()}
    return price_string, original_percents, by_position


async def reprice_existing_build(
    session: AsyncSession, shop: str, *, recommendation, ratios: dict[str, Any], name: Any = None
) -> dict[str, Any]:
    """Port of api.save-build.jsx's action -- re-price/create-a-variant for a NEW ratio on an
    EXISTING product. Deliberately never mutates a shared variant's price in place: Shopify carts
    always display a variant's *current* price, so two different saved ratios sharing one variant
    would silently change the price of an item already sitting in someone else's cart. Every
    distinct ratio (outside MATCH_TOLERANCE_PCT of an existing one) gets its own variant instead.

    The caller must already have authorized `recommendation` (build capability). The product id is
    taken from the recommendation row, never from the caller. No write happens until the product
    has been verified as this recommendation's own build and the inputs and price are valid.
    """
    # ---- VALIDATE INPUT (cheap, before any network) ----
    shop = require_trusted_shop(shop)
    ratios = validate_ratios(ratios)
    name = validate_custom_name(name)
    if not recommendation.shopifyProductId:
        raise BuildProductNotSaved("This fragrance hasn't been created yet — please save it from the preview first.")
    product_id = recommendation.shopifyProductId

    # ---- READ ----
    product = await get_product_for_pricing(session, shop, product_id)

    # ---- AUTHORIZE / VALIDATE PRODUCT ----
    composition = verify_build_product(product, recommendation)
    variant_edges = product["variants"]["edges"]

    # ---- COMPUTE + CONFIRM RESULT IS SAFE ----
    price_string, original_percents, _ = _compute_reprice(composition["layers"], ratios)

    closest_edge = None
    closest_distance = float("inf")
    for edge in variant_edges:
        distance = 0.0
        for opt in edge["node"]["selectedOptions"]:
            position = OPTION_NAME_TO_POSITION.get(opt["name"])
            if not position:
                continue
            variant_pct = _extract_ratio_percent(opt["value"])
            if variant_pct is None:
                variant_pct = original_percents.get(position, 0)
            distance = max(distance, abs(variant_pct - ratios[position]))
        if distance < closest_distance:
            closest_distance = distance
            closest_edge = edge

    # ---- WRITE (only now) ----
    if closest_edge is not None and closest_distance <= MATCH_TOLERANCE_PCT:
        inventory_item = closest_edge["node"].get("inventoryItem") or {}
        if inventory_item.get("tracked"):
            await set_inventory_item_untracked(session, shop, inventory_item.get("id"))
        await _rename_if_changed(session, shop, product, name)
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
    await _rename_if_changed(session, shop, product, name)
    return {"price": new_variant["price"], "variantId": new_variant["id"], "created": True}


async def _rename_if_changed(session: AsyncSession, shop: str, product: dict[str, Any], name: str | None) -> None:
    """The rename is always the last write, and only when a validated, different name was given."""
    if name is None or name == product.get("title"):
        return
    await rename_product(session, shop, product["id"], name)
