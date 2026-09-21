"""The single inventory gate for commerce actions (Phase 5, finding F9).

Four different states must never be confused:

  1. a fragrance direction MATCHES the customer           (recommendation engine)
  2. its manufacturing inputs were VERIFIED on hand        (this module)
  3. stock is RESERVED                                     (NOT SUPPORTED anywhere in this system)
  4. a purchase / manufacturing commitment was ACCEPTED    (Shopify checkout; outside this repo)

This module establishes (2) and only (2), for one specific build, at one moment. It never
reserves anything. A lookup is not a reservation: another sales channel, another customer, or the
lab can consume the same oil a millisecond later.

Recommendation-time inventory (odoo_inventory.evaluate_candidate_inventory, its 60 s cache and the
RecommendationInventorySnapshot row) has deliberately lenient "unknown does not reject" semantics
so discovery keeps working during an outage. NONE of that is consulted here. Commerce uses its own
fresh, uncached, strictly validated lookup, and everything that is not a complete positive answer
fails closed.

What the source of truth actually tells us (see docs/INVENTORY_COMMERCE_SECURITY.md):
  * identity: recommendation.productsJson title -> FragranceProduct.normalizedTitle (unique)
    -> OdooOilMapping (unique per product, must be active) -> odooSku (Odoo default_code);
  * quantity: `on_hand_qty` only. No reserved/available split, no forecast, no warehouse or
    company parameter, no unit field, no pagination marker;
  * unit: NOT reported by the API. The only unit evidence is OdooOilMapping.unitOfMeasure, which an
    operator records per mapping. Anything other than millilitres (or nothing recorded) is UNKNOWN
    here: no conversion factor is invented.

Requirement (docs section 3): how the customer's Top/Middle/Base slider changes the per-oil
quantities the lab pours is NOT defined anywhere in this repository. Rather than invent that
mapping, the gate uses a bound that holds for every valid ratio: a bottle holds at most
formulas.MAX_OIL_ML of oil in total, so no single oil (and no set of components sharing one Odoo
item) can need more than MAX_OIL_ML per bottle. Every distinct Odoo item in the build must have at
least MAX_OIL_ML x quantity on hand. This is a sufficient condition, not the exact requirement; it
can refuse a build that the lab could in fact make from a nearly empty oil.
"""

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import FragranceProduct, OdooOilMapping
from app.db.time import utcnow
from app.fragrance.formulas import FINISHED_BOTTLE_ML, MAX_OIL_ML, build_production_formula
from app.fragrance.normalization import normalize_product_name
from app.integrations import odoo_client
from app.shopify.build_input import POSITIONS, validate_ratios

logger = logging.getLogger(__name__)

MAX_COMMERCE_QUANTITY = 10
_MAX_COMPONENTS = 4
_ML_UNITS = {"ml", "milliliter", "milliliters", "millilitre", "millilitres"}
_TWO_PLACES = Decimal("0.01")


class InventoryState(str, Enum):
    VERIFIED_AVAILABLE = "VERIFIED_AVAILABLE"        # every item: valid on-hand quantity >= requirement, just now
    VERIFIED_INSUFFICIENT = "VERIFIED_INSUFFICIENT"  # a complete valid answer says at least one item is short
    UNKNOWN = "UNKNOWN"                              # cannot be established (mapping, unit, quantity, response)
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"      # the inventory service did not give a usable answer


# Internal reason codes (logs only; never returned to a customer or a model).
REQUIREMENTS_UNKNOWN = "REQUIREMENTS_UNKNOWN"
MAPPING_MISSING = "MAPPING_MISSING"
MAPPING_INACTIVE = "MAPPING_INACTIVE"
UNIT_UNCONFIRMED = "UNIT_UNCONFIRMED"
SERVICE_ERROR = "SERVICE_ERROR"
RESPONSE_MALFORMED = "RESPONSE_MALFORMED"
RESPONSE_INCOMPLETE = "RESPONSE_INCOMPLETE"
RESPONSE_AMBIGUOUS = "RESPONSE_AMBIGUOUS"
QUANTITY_INVALID = "QUANTITY_INVALID"
INSUFFICIENT = "INSUFFICIENT"
STALE = "STALE"
FINGERPRINT_MISMATCH = "FINGERPRINT_MISMATCH"


class RequirementsUnknown(Exception):
    """The authoritative recommendation does not carry enough valid data to know what must be on hand."""


@dataclass(frozen=True)
class BuildRequirements:
    """What one specific commerce action needs, derived ONLY from server-side data."""

    recommendation_id: str
    component_titles: tuple[str, ...]             # normalized, sorted; internal only
    ratios: tuple[tuple[str, int], ...]           # the customer's validated Top/Middle/Base
    quantity: int
    required_ml_per_item: Decimal                 # worst-case bound per distinct Odoo item
    default_formula_ml: tuple[tuple[str, float], ...]  # informational: the recommendation's own 13 ml formula
    fingerprint: str


@dataclass(frozen=True)
class InventoryVerification:
    state: InventoryState
    reasons: tuple[str, ...]
    fingerprint: str
    checked_at: datetime
    item_count: int = 0
    _issued: bool = field(default=False, repr=False)

    def authorizes(self, fingerprint: str, *, now: datetime | None = None) -> tuple[bool, str | None]:
        """True only for a positive verification of EXACTLY this build that is still fresh."""
        if not self._issued or self.state is not InventoryState.VERIFIED_AVAILABLE:
            return False, self.reasons[0] if self.reasons else self.state.value
        if fingerprint != self.fingerprint:
            return False, FINGERPRINT_MISMATCH
        age = (now or utcnow()) - self.checked_at
        if age < timedelta(0) or age > timedelta(seconds=settings.commerce_inventory_max_age_seconds):
            return False, STALE
        return True, None


class InventoryNotVerified(Exception):
    """Raised by require_commerce_inventory. Carries only the typed state; routes map it to a
    customer-safe response with commerce_failure()."""

    def __init__(self, state: InventoryState, reason: str):
        super().__init__(state.value)
        self.state = state
        self.reason = reason


# ---------------------------------------------------------------------------
# Requirements
# ---------------------------------------------------------------------------

def compute_build_requirements(recommendation: Any, ratios: Any, *, quantity: int = 1) -> BuildRequirements:
    """Deterministic, server-side only. Nothing the browser sends about availability, component
    quantities, mappings, timestamps, prices or source products is ever read."""
    ratios = validate_ratios(ratios)  # Phase 1 rules unchanged: integers 1..98, exactly three layers, sum 100
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= MAX_COMMERCE_QUANTITY:
        raise RequirementsUnknown("quantity")

    products = recommendation.productsJson if isinstance(recommendation.productsJson, list) else None
    if not products or len(products) > _MAX_COMPONENTS:
        raise RequirementsUnknown("components")
    titles: list[str] = []
    for product in products:
        title = product.get("title") if isinstance(product, dict) else None
        if not isinstance(title, str) or not title.strip():
            raise RequirementsUnknown("component_title")
        titles.append(normalize_product_name(title))
    if len(set(titles)) != len(titles):
        raise RequirementsUnknown("duplicate_component")

    # The recommendation's own per-product formula must be coherent (same products, finite,
    # non-negative, totalling 100%). It is validated with the existing production formula and
    # kept as information; the bound below does not depend on it.
    ratios_by_product = recommendation.ratiosJson if isinstance(recommendation.ratiosJson, list) else None
    if not ratios_by_product or len(ratios_by_product) != len(products):
        raise RequirementsUnknown("formula")
    formula_input = []
    for row in ratios_by_product:
        title = row.get("productTitle") if isinstance(row, dict) else None
        percent = row.get("ratioPercent") if isinstance(row, dict) else None
        if not isinstance(title, str) or isinstance(percent, bool) or not isinstance(percent, (int, float)) or not math.isfinite(percent) or percent <= 0:
            raise RequirementsUnknown("formula")
        if normalize_product_name(title) not in titles:
            raise RequirementsUnknown("formula_component_mismatch")
        formula_input.append({"productTitle": title, "ratioPercent": percent})
    try:
        formula = build_production_formula(formula_input)
    except ValueError:
        raise RequirementsUnknown("formula") from None

    required = (Decimal(MAX_OIL_ML) * quantity).quantize(_TWO_PLACES)
    ordered_titles = tuple(sorted(titles))
    ordered_ratios = tuple((position, ratios[position]) for position in POSITIONS)
    fingerprint = hashlib.sha256(json.dumps({
        "recommendationId": recommendation.id, "components": ordered_titles,
        "formula": sorted((normalize_product_name(c["productTitle"]), c["ratioPercent"]) for c in formula["components"]),
        "ratios": ordered_ratios, "quantity": quantity, "bottleMl": FINISHED_BOTTLE_ML, "maxOilMl": MAX_OIL_ML,
    }, sort_keys=True).encode()).hexdigest()
    return BuildRequirements(
        recommendation_id=recommendation.id, component_titles=ordered_titles, ratios=ordered_ratios, quantity=quantity,
        required_ml_per_item=required,
        default_formula_ml=tuple((normalize_product_name(c["productTitle"]), c["requiredOilMl"]) for c in formula["components"]),
        fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _result(state: InventoryState, reasons: list[str], requirements_fingerprint: str, item_count: int = 0) -> InventoryVerification:
    return InventoryVerification(state=state, reasons=tuple(dict.fromkeys(reasons)), fingerprint=requirements_fingerprint, checked_at=utcnow(), item_count=item_count, _issued=True)


def _valid_quantity(value: Any) -> Decimal | None:
    """A usable on-hand quantity: a real JSON number, finite, not negative. Booleans, strings,
    null, NaN and infinities are all invalid -- never coerced."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        quantity = Decimal(str(value))
    except InvalidOperation:
        return None
    return quantity if quantity >= 0 else None


async def _resolve_items(session: AsyncSession, requirements: BuildRequirements) -> tuple[dict[str, int], list[str]]:
    """component -> Odoo item. Returns ({sku: number of components using it}, reasons)."""
    reasons: list[str] = []
    components_per_sku: dict[str, int] = {}
    for normalized_title in requirements.component_titles:
        product = await session.scalar(select(FragranceProduct).where(FragranceProduct.normalizedTitle == normalized_title))
        mapping = await session.scalar(select(OdooOilMapping).where(OdooOilMapping.fragranceProductId == product.id)) if product else None
        if not product or not mapping or not isinstance(mapping.odooSku, str) or not mapping.odooSku.strip():
            reasons.append(MAPPING_MISSING)
            continue
        if not mapping.active:
            reasons.append(MAPPING_INACTIVE)
            continue
        if (mapping.unitOfMeasure or "").strip().lower() not in _ML_UNITS:
            reasons.append(UNIT_UNCONFIRMED)
            continue
        components_per_sku[mapping.odooSku] = components_per_sku.get(mapping.odooSku, 0) + 1
    return components_per_sku, reasons


async def verify_build_inventory(session: AsyncSession, *, recommendation: Any, ratios: Any, quantity: int = 1) -> tuple[BuildRequirements | None, InventoryVerification]:
    """One fresh, uncached, batched lookup for exactly this build. Never raises for inventory
    reasons; returns a typed result. Every component must be resolved and sufficient: one good
    component never hides a missing one."""
    try:
        requirements = compute_build_requirements(recommendation, ratios, quantity=quantity)
    except RequirementsUnknown:
        return None, _result(InventoryState.UNKNOWN, [REQUIREMENTS_UNKNOWN], "")

    components_per_sku, reasons = await _resolve_items(session, requirements)
    if reasons:
        # No lookup at all: a partial check could only ever be misread as partial approval.
        return requirements, _result(InventoryState.UNKNOWN, reasons, requirements.fingerprint, len(components_per_sku))

    skus = sorted(components_per_sku)
    try:
        response = await odoo_client.get_inventory_by_skus(skus)
    except Exception:  # noqa: BLE001 -- the client normally returns ok=False; never let it raise through
        response = {"ok": False}
    if not isinstance(response, dict) or not response.get("ok"):
        return requirements, _result(InventoryState.SERVICE_UNAVAILABLE, [SERVICE_ERROR], requirements.fingerprint, len(skus))

    body = response.get("json")
    rows = body.get("products") if isinstance(body, dict) else None
    if not isinstance(body, dict) or body.get("success") is not True or not isinstance(rows, list):
        return requirements, _result(InventoryState.SERVICE_UNAVAILABLE, [RESPONSE_MALFORMED], requirements.fingerprint, len(skus))

    on_hand: dict[str, Decimal] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("default_code"), str):
            reasons.append(RESPONSE_MALFORMED)
            continue
        sku = row["default_code"]
        if sku not in components_per_sku:
            continue  # an item nobody asked about proves nothing
        if sku in on_hand:
            reasons.append(RESPONSE_AMBIGUOUS)  # two rows for one item: which one is true?
            continue
        quantity_value = _valid_quantity(row.get("on_hand_qty"))
        if quantity_value is None:
            reasons.append(QUANTITY_INVALID)
            continue
        on_hand[sku] = quantity_value
    if any(sku not in on_hand for sku in skus) and not reasons:
        reasons.append(RESPONSE_INCOMPLETE)  # a requested item absent from the answer (or a truncated batch)
    if reasons:
        return requirements, _result(InventoryState.UNKNOWN, reasons, requirements.fingerprint, len(skus))

    # Demand is aggregated per Odoo item. The bound already covers every component that shares an
    # item: all of them together cannot exceed the bottle's total oil.
    if any(on_hand[sku] < requirements.required_ml_per_item for sku in skus):
        return requirements, _result(InventoryState.VERIFIED_INSUFFICIENT, [INSUFFICIENT], requirements.fingerprint, len(skus))
    return requirements, _result(InventoryState.VERIFIED_AVAILABLE, [], requirements.fingerprint, len(skus))


async def require_commerce_inventory(session: AsyncSession, *, recommendation: Any, ratios: Any, quantity: int = 1) -> InventoryVerification:
    """THE gate. Called by the Shopify write layer (app/shopify/builds.py) immediately before the
    first write of every commerce path, so no route and no future service caller can skip it.
    There is no setting that turns UNKNOWN into approval."""
    requirements, verification = await verify_build_inventory(session, recommendation=recommendation, ratios=ratios, quantity=quantity)
    allowed, reason = verification.authorizes(requirements.fingerprint if requirements else "\x00")
    logger.info("COMMERCE_INVENTORY_DECISION %s", json.dumps({
        "recommendationId": getattr(recommendation, "id", None), "state": verification.state.value, "allowed": allowed,
        "reasons": list(verification.reasons) or ([reason] if reason else []), "itemCount": verification.item_count,
    }))
    if not allowed:
        state = verification.state if verification.state is not InventoryState.VERIFIED_AVAILABLE else InventoryState.UNKNOWN
        raise InventoryNotVerified(state, reason or state.value)
    return verification


# ---------------------------------------------------------------------------
# Customer-safe failure responses (stable codes; no operational detail, no retry durations)
# ---------------------------------------------------------------------------

COMMERCE_FAILURES: dict[str, tuple[int, str, str]] = {
    InventoryState.VERIFIED_INSUFFICIENT.value: (409, "inventory_insufficient", "This blend can't be made right now because one of its ingredients is running low. Your design is saved, and you can adjust it or try again later."),
    InventoryState.UNKNOWN.value: (409, "inventory_unconfirmed", "We couldn't confirm that every ingredient for this blend is available, so we haven't created it. Your design is saved."),
    InventoryState.SERVICE_UNAVAILABLE.value: (503, "inventory_unavailable", "We can't check ingredient availability right now, so we haven't created this blend. Your design is saved. Please try again shortly."),
    "build_in_progress": (409, "build_in_progress", "This blend is already being saved. Please wait a moment before trying again."),
    "build_pending_review": (409, "build_pending_review", "We couldn't confirm whether this blend finished saving, so we've paused it rather than risk creating it twice. Please contact us and we'll sort it out."),
}


def commerce_failure(key: str | InventoryState) -> tuple[int, dict[str, str]]:
    status, code, message = COMMERCE_FAILURES[key.value if isinstance(key, InventoryState) else key]
    return status, {"error": message, "code": code}
