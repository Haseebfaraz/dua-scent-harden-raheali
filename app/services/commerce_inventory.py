"""The single inventory gate for commerce actions (Phase 5, corrected in Phase 5A; finding F9).

Four different states must never be confused:

  1. a fragrance direction MATCHES the customer           (recommendation engine)
  2. the commerce inventory POLICY was satisfied           (this module)
  3. stock is RESERVED                                     (NOT SUPPORTED anywhere in this system)
  4. a purchase / manufacturing commitment was ACCEPTED    (Shopify checkout; outside this repo)

Phase 5A separates three things the first version blurred into one "VERIFIED_AVAILABLE":

  * what the source REPORTED        -> StockObservation  (a number came back; nothing more)
  * what has actually been VERIFIED -> the facts list below
  * whether that satisfies POLICY   -> InventoryDecision.state

A fresh positive quantity is only an observation. It does not prove that the right location was
queried, that the unit matches, that the stock is unreserved, or that the bound used for the
requirement is true of manufacturing. RULE: if a fact necessary for the commerce decision is
unknown, the decision is UNCONFIRMED and the dependent write is blocked. It is acceptable, and
currently expected, for real commerce to stay blocked until the missing facts are supplied by the
integration (docs/INVENTORY_COMMERCE_SECURITY.md sections 2a and 10).

Facts required for POLICY_SATISFIED, and where each comes from:

  F1 integration configured     server config ODOO_INVENTORY_URL (no built-in default)
  F2 source location            server config ODOO_INVENTORY_LOCATION_SCOPE  AND the response must
                                echo exactly that value in its top-level "location"
  F3 reservation semantics      server config ODOO_INVENTORY_QUANTITY_SEMANTICS must be
                                UNRESERVED_AVAILABLE AND each row must carry "available_qty".
                                ("on_hand_qty" alone can include stock already promised.)
  F4 unit                       OdooOilMapping.unitOfMeasure says millilitres AND any "uom" the
                                row carries agrees. No conversion is ever performed.
  F5 manufacturing contract     server config MANUFACTURING_MAX_OIL_ML_PER_BOTTLE (see below)
  F6 component -> item mapping  database: unique product row -> unique active mapping
  F7 complete valid response    every requested item present exactly once with a valid quantity
  F8 sufficiency                every distinct item's available quantity covers the bound
  F9 freshness + binding        taken just now, for exactly this operation's fingerprint

None of F1..F5 can approve anything alone, none is read from a browser, a model or a request, and
there is no setting that skips F7/F8. Even POLICY_SATISFIED is NOT a reservation and says nothing
about purchases that do not pass through this backend.

The requirement bound (docs section 3): how the customer's Top/Middle/Base slider changes the
per-oil quantities is not defined in this repository, and no rule was invented. The bound rests on
ONE premise that must be supplied as the manufacturing contract (F5): producing one bottle draws at
most M millilitres of fragrance oil from inventory in total, for every supported build, loss and
overfill included. Given that premise, no single item (and no group of components sharing an item)
can need more than M, so "every item has at least M available" is a SUFFICIENT consumption
condition. It deliberately rejects builds a nearly empty oil could still supply. It proves nothing
about reserved or inaccessible stock (that is F2/F3), about concurrent consumption, or about
inputs other than fragrance oils. Without F5 the requirement is UNKNOWN and commerce is blocked.
"""

import hashlib
import json
import logging
import math
from dataclasses import dataclass
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

# The product offers exactly one 34 ml bottle per action (the cart link is always ":1").
SUPPORTED_QUANTITY = 1
_MAX_COMPONENTS = 4
_ML_UNITS = {"ml", "milliliter", "milliliters", "millilitre", "millilitres"}
_TWO_PLACES = Decimal("0.01")
QUANTITY_SEMANTICS_UNRESERVED = "UNRESERVED_AVAILABLE"
QUANTITY_SEMANTICS_ON_HAND = "ON_HAND_INCLUDES_RESERVED"
_SEAL = object()  # only decisions built inside this module carry it


def _now() -> datetime:
    """The clock, isolated so tests can control it."""
    return utcnow()


class InventoryState(str, Enum):
    POLICY_SATISFIED = "POLICY_SATISFIED"        # F1..F9 all hold, just now, for exactly this operation
    INSUFFICIENT = "INSUFFICIENT"                # all facts known; a complete valid answer says an item is short
    UNCONFIRMED = "UNCONFIRMED"                  # at least one necessary fact is unknown
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"  # no usable answer from the source


class ReportedStock(str, Enum):
    """What the SOURCE said, independent of whether policy accepts it."""

    NOT_OBSERVED = "NOT_OBSERVED"
    SUFFICIENT_REPORTED = "SUFFICIENT_REPORTED"
    SHORTAGE_REPORTED = "SHORTAGE_REPORTED"


# Internal reason codes (logs only; never returned to a customer or a model).
INTEGRATION_NOT_CONFIGURED = "INTEGRATION_NOT_CONFIGURED"
SOURCE_SCOPE_UNDECLARED = "SOURCE_SCOPE_UNDECLARED"
SOURCE_SCOPE_UNCONFIRMED = "SOURCE_SCOPE_UNCONFIRMED"
RESERVATION_SEMANTICS_UNDECLARED = "RESERVATION_SEMANTICS_UNDECLARED"
RESERVATION_SEMANTICS_INSUFFICIENT = "RESERVATION_SEMANTICS_INSUFFICIENT"
MANUFACTURING_CONTRACT_MISSING = "MANUFACTURING_CONTRACT_MISSING"
MANUFACTURING_CONTRACT_INVALID = "MANUFACTURING_CONTRACT_INVALID"
REQUIREMENTS_UNKNOWN = "REQUIREMENTS_UNKNOWN"
MAPPING_MISSING = "MAPPING_MISSING"
MAPPING_INACTIVE = "MAPPING_INACTIVE"
UNIT_UNCONFIRMED = "UNIT_UNCONFIRMED"
UNIT_INCONSISTENT = "UNIT_INCONSISTENT"
SERVICE_ERROR = "SERVICE_ERROR"
RESPONSE_MALFORMED = "RESPONSE_MALFORMED"
RESPONSE_INCOMPLETE = "RESPONSE_INCOMPLETE"
RESPONSE_AMBIGUOUS = "RESPONSE_AMBIGUOUS"
QUANTITY_INVALID = "QUANTITY_INVALID"
INSUFFICIENT = "INSUFFICIENT"
STALE = "STALE"
FINGERPRINT_MISMATCH = "FINGERPRINT_MISMATCH"
NOT_ISSUED_BY_GATE = "NOT_ISSUED_BY_GATE"


class RequirementsUnknown(Exception):
    """What must be available cannot be established from trusted server-side data."""

    def __init__(self, detail: str, reason: str = REQUIREMENTS_UNKNOWN):
        super().__init__(detail)
        self.reason = reason


@dataclass(frozen=True)
class BuildRequirements:
    """What one specific commerce action needs, derived ONLY from server-side data."""

    recommendation_id: str
    component_titles: tuple[str, ...]             # normalized, sorted; internal only
    ratios: tuple[tuple[str, int], ...]           # the customer's validated Top/Middle/Base
    quantity: int
    required_ml_per_item: Decimal                 # the contract bound per distinct inventory item
    default_formula_ml: tuple[tuple[str, float], ...]  # informational: the recommendation's own formula
    fingerprint: str                              # recipe + ratios + quantity + bottle + bound


@dataclass(frozen=True)
class StockObservation:
    """What the source reported. An observation, never an approval."""

    reported: ReportedStock = ReportedStock.NOT_OBSERVED
    quantity_field: str | None = None      # which response field was read
    location_echoed: bool = False          # the response named the declared location
    item_count: int = 0


@dataclass(frozen=True)
class InventoryDecision:
    """The POLICY decision for one operation. Built only by this module (see _SEAL); the Shopify
    write layer never accepts one from a caller, it always obtains its own."""

    state: InventoryState
    reasons: tuple[str, ...]
    observation: StockObservation
    operation_fingerprint: str
    checked_at: datetime
    _seal: object = None

    def authorizes(self, operation_fingerprint: str, *, now: datetime | None = None) -> tuple[bool, str | None]:
        """True only for a sealed POLICY_SATISFIED decision for EXACTLY this operation, still fresh."""
        if self._seal is not _SEAL:
            return False, NOT_ISSUED_BY_GATE
        if self.state is not InventoryState.POLICY_SATISFIED:
            return False, self.reasons[0] if self.reasons else self.state.value
        if not operation_fingerprint or operation_fingerprint != self.operation_fingerprint:
            return False, FINGERPRINT_MISMATCH
        age = (now or _now()) - self.checked_at
        if age < timedelta(0) or age > timedelta(seconds=settings.commerce_inventory_max_age_seconds):
            return False, STALE
        return True, None


class InventoryNotVerified(Exception):
    """Raised by the gate. Carries only the typed state; routes map it with commerce_failure()."""

    def __init__(self, state: InventoryState, reason: str):
        super().__init__(state.value)
        self.state = state
        self.reason = reason


# ---------------------------------------------------------------------------
# Requirements
# ---------------------------------------------------------------------------

def manufacturing_bound_ml() -> Decimal:
    """F5. The declared manufacturing contract, validated against the formula this repository
    already owns. Raises RequirementsUnknown when it is missing or contradicts the formula."""
    declared = settings.manufacturing_max_oil_ml_per_bottle
    if declared is None:
        raise RequirementsUnknown("manufacturing contract not declared", MANUFACTURING_CONTRACT_MISSING)
    if isinstance(declared, bool) or not isinstance(declared, (int, float)) or not math.isfinite(declared):
        raise RequirementsUnknown("manufacturing contract invalid", MANUFACTURING_CONTRACT_INVALID)
    # Less than the formula's own maximum oil volume would contradict formulas.py; more than the
    # bottle is impossible. Either way the declaration cannot be trusted.
    if declared < MAX_OIL_ML or declared > FINISHED_BOTTLE_ML:
        raise RequirementsUnknown("manufacturing contract out of range", MANUFACTURING_CONTRACT_INVALID)
    return Decimal(str(declared)).quantize(_TWO_PLACES)


def compute_build_requirements(recommendation: Any, ratios: Any, *, quantity: int = SUPPORTED_QUANTITY) -> BuildRequirements:
    """Deterministic, server-side only. Nothing the browser sends about availability, component
    quantities, mappings, timestamps, prices or source products is ever read."""
    ratios = validate_ratios(ratios)  # Phase 1 rules unchanged: integers 1..98, exactly three layers, sum 100
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity != SUPPORTED_QUANTITY:
        raise RequirementsUnknown("quantity")  # the product sells one bottle per action; nothing else is supported

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

    bound = manufacturing_bound_ml()
    required = (bound * quantity).quantize(_TWO_PLACES)
    ordered_titles = tuple(sorted(titles))
    ordered_ratios = tuple((position, ratios[position]) for position in POSITIONS)
    fingerprint = hashlib.sha256(json.dumps({
        "recommendationId": recommendation.id, "components": ordered_titles,
        "formula": sorted((normalize_product_name(c["productTitle"]), c["ratioPercent"]) for c in formula["components"]),
        "ratios": ordered_ratios, "quantity": quantity, "bottleMl": FINISHED_BOTTLE_ML, "boundMl": str(bound),
    }, sort_keys=True).encode()).hexdigest()
    return BuildRequirements(
        recommendation_id=recommendation.id, component_titles=ordered_titles, ratios=ordered_ratios, quantity=quantity,
        required_ml_per_item=required,
        default_formula_ml=tuple((normalize_product_name(c["productTitle"]), c["requiredOilMl"]) for c in formula["components"]),
        fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# Source contract (F1..F3) and mapping (F4, F6)
# ---------------------------------------------------------------------------

def source_contract_gaps() -> list[str]:
    """Declared facts about the source that are missing or do not satisfy the policy."""
    gaps: list[str] = []
    if not odoo_client.inventory_integration_configured():
        gaps.append(INTEGRATION_NOT_CONFIGURED)
    if not (settings.odoo_inventory_location_scope or "").strip():
        gaps.append(SOURCE_SCOPE_UNDECLARED)
    semantics = (settings.odoo_inventory_quantity_semantics or "").strip()
    if not semantics or semantics not in (QUANTITY_SEMANTICS_UNRESERVED, QUANTITY_SEMANTICS_ON_HAND):
        gaps.append(RESERVATION_SEMANTICS_UNDECLARED)
    elif semantics != QUANTITY_SEMANTICS_UNRESERVED:
        gaps.append(RESERVATION_SEMANTICS_INSUFFICIENT)
    return gaps


async def _resolve_items(session: AsyncSession, requirements: BuildRequirements) -> tuple[dict[str, int], list[tuple[str, str]], list[str]]:
    """component -> inventory item. Returns ({item: components using it}, [(title, item)], reasons)."""
    reasons: list[str] = []
    components_per_item: dict[str, int] = {}
    resolved: list[tuple[str, str]] = []
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
        components_per_item[mapping.odooSku] = components_per_item.get(mapping.odooSku, 0) + 1
        resolved.append((normalized_title, mapping.odooSku))
    return components_per_item, resolved, reasons


def _operation_fingerprint(requirements: BuildRequirements, resolved: list[tuple[str, str]]) -> str:
    """Binds a decision to the recipe, ratios, quantity, bottle and bound (requirements), to the
    component -> item mapping used, and to the declared source (location + quantity semantics)."""
    return hashlib.sha256(json.dumps({
        "requirements": requirements.fingerprint, "mapping": sorted(resolved),
        "location": (settings.odoo_inventory_location_scope or "").strip(),
        "semantics": (settings.odoo_inventory_quantity_semantics or "").strip(),
    }, sort_keys=True).encode()).hexdigest()


def _decision(state: InventoryState, reasons: list[str], fingerprint: str, observation: StockObservation | None = None) -> InventoryDecision:
    return InventoryDecision(state=state, reasons=tuple(dict.fromkeys(reasons)), observation=observation or StockObservation(), operation_fingerprint=fingerprint, checked_at=_now(), _seal=_SEAL)


def _valid_quantity(value: Any) -> Decimal | None:
    """A usable quantity: a real JSON number, finite, not negative. Booleans, strings, null, NaN
    and infinities are all invalid -- never coerced."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        quantity = Decimal(str(value))
    except InvalidOperation:
        return None
    return quantity if quantity >= 0 else None


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

async def verify_build_inventory(session: AsyncSession, *, recommendation: Any, ratios: Any, quantity: int = SUPPORTED_QUANTITY) -> tuple[str, InventoryDecision]:
    """One fresh, uncached, batched lookup for exactly this operation. Never raises for inventory
    reasons. Returns (operation_fingerprint, decision). When any necessary fact is unknown the
    source is not even queried: a number obtained without the facts could only be misread."""
    try:
        requirements = compute_build_requirements(recommendation, ratios, quantity=quantity)
    except RequirementsUnknown as err:
        return "", _decision(InventoryState.UNCONFIRMED, [err.reason], "")

    gaps = source_contract_gaps()
    components_per_item, resolved, mapping_reasons = await _resolve_items(session, requirements)
    fingerprint = _operation_fingerprint(requirements, resolved)
    if gaps or mapping_reasons:
        return fingerprint, _decision(InventoryState.UNCONFIRMED, [*gaps, *mapping_reasons], fingerprint)

    items = sorted(components_per_item)
    try:
        response = await odoo_client.get_inventory_by_skus(items)
    except Exception:  # noqa: BLE001 -- the client normally returns ok=False; never let it raise through
        response = {"ok": False}
    if not isinstance(response, dict) or not response.get("ok"):
        return fingerprint, _decision(InventoryState.SERVICE_UNAVAILABLE, [SERVICE_ERROR], fingerprint)

    body = response.get("json")
    rows = body.get("products") if isinstance(body, dict) else None
    if not isinstance(body, dict) or body.get("success") is not True or not isinstance(rows, list):
        return fingerprint, _decision(InventoryState.SERVICE_UNAVAILABLE, [RESPONSE_MALFORMED], fingerprint)

    reasons: list[str] = []
    location_echoed = isinstance(body.get("location"), str) and body["location"] == settings.odoo_inventory_location_scope.strip()
    if not location_echoed:
        reasons.append(SOURCE_SCOPE_UNCONFIRMED)  # F2: a declaration nobody echoes proves nothing

    available: dict[str, Decimal] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("default_code"), str):
            reasons.append(RESPONSE_MALFORMED)
            continue
        item = row["default_code"]
        if item not in components_per_item:
            continue  # an item nobody asked about proves nothing
        if item in available:
            reasons.append(RESPONSE_AMBIGUOUS)
            continue
        if "uom" in row and (not isinstance(row["uom"], str) or row["uom"].strip().lower() not in _ML_UNITS):
            reasons.append(UNIT_INCONSISTENT)  # F4: the source disagrees with the recorded unit
            continue
        if "available_qty" not in row:
            reasons.append(RESERVATION_SEMANTICS_INSUFFICIENT)  # F3: only on-hand was reported
            continue
        value = _valid_quantity(row.get("available_qty"))
        if value is None:
            reasons.append(QUANTITY_INVALID)
            continue
        available[item] = value
    if any(item not in available for item in items) and not [r for r in reasons if r != SOURCE_SCOPE_UNCONFIRMED]:
        reasons.append(RESPONSE_INCOMPLETE)

    complete = all(item in available for item in items)
    reported = ReportedStock.NOT_OBSERVED
    if complete:
        reported = ReportedStock.SUFFICIENT_REPORTED if all(available[i] >= requirements.required_ml_per_item for i in items) else ReportedStock.SHORTAGE_REPORTED
    observation = StockObservation(reported=reported, quantity_field="available_qty", location_echoed=location_echoed, item_count=len(items))

    if reasons:
        # The source may well have reported plenty; without the facts that is still not approval.
        return fingerprint, _decision(InventoryState.UNCONFIRMED, reasons, fingerprint, observation)
    if reported is ReportedStock.SHORTAGE_REPORTED:
        return fingerprint, _decision(InventoryState.INSUFFICIENT, [INSUFFICIENT], fingerprint, observation)
    return fingerprint, _decision(InventoryState.POLICY_SATISFIED, [], fingerprint, observation)


async def require_commerce_inventory(session: AsyncSession, *, recommendation: Any, ratios: Any, quantity: int = SUPPORTED_QUANTITY) -> InventoryDecision:
    """THE gate. Called by the Shopify write layer (app/shopify/builds.py) immediately before the
    first write of every commerce path, so no route and no future service caller can skip it. It
    takes no decision object from anyone: it always performs its own verification."""
    fingerprint, decision = await verify_build_inventory(session, recommendation=recommendation, ratios=ratios, quantity=quantity)
    allowed, reason = decision.authorizes(fingerprint)
    logger.info("COMMERCE_INVENTORY_DECISION %s", json.dumps({
        "recommendationId": getattr(recommendation, "id", None), "state": decision.state.value, "allowed": allowed,
        "reasons": list(decision.reasons) or ([reason] if reason else []), "reported": decision.observation.reported.value,
        "itemCount": decision.observation.item_count,
    }))
    if not allowed:
        state = decision.state if decision.state is not InventoryState.POLICY_SATISFIED else InventoryState.UNCONFIRMED
        raise InventoryNotVerified(state, reason or state.value)
    return decision


async def ensure_still_satisfied(session: AsyncSession, decision: InventoryDecision, *, recommendation: Any, ratios: Any, quantity: int = SUPPORTED_QUANTITY) -> InventoryDecision:
    """For a multi-step operation: before the step that makes a build purchasable, the evidence
    must still be inside its freshness window. If it has expired it is NOT silently reused: one
    new verification is made, and anything but POLICY_SATISFIED raises."""
    if decision.authorizes(decision.operation_fingerprint)[0]:
        return decision
    return await require_commerce_inventory(session, recommendation=recommendation, ratios=ratios, quantity=quantity)


# ---------------------------------------------------------------------------
# Customer-safe failure responses (stable codes; no operational detail, no retry durations)
# ---------------------------------------------------------------------------
# Preflight refusals may say nothing was created, because nothing was attempted. The
# pending-review message must NOT: a remote object may exist.

COMMERCE_FAILURES: dict[str, tuple[int, str, str]] = {
    InventoryState.INSUFFICIENT.value: (409, "inventory_insufficient", "This blend can't be made right now because one of its ingredients is running low. Your design is saved, and you can adjust it or try again later."),
    InventoryState.UNCONFIRMED.value: (409, "inventory_unconfirmed", "We couldn't confirm that every ingredient for this blend is available, so we haven't created it. Your design is saved."),
    InventoryState.SERVICE_UNAVAILABLE.value: (503, "inventory_unavailable", "We can't check ingredient availability right now, so we haven't created this blend. Your design is saved. Please try again shortly."),
    "build_in_progress": (409, "build_in_progress", "This blend is already being saved. Please wait a moment before trying again."),
    "build_pending_review": (409, "build_pending_review", "We couldn't confirm whether this blend finished saving, so we've paused it rather than risk creating it twice. Please contact us and we'll sort it out."),
}


def commerce_failure(key: str | InventoryState) -> tuple[int, dict[str, str]]:
    status, code, message = COMMERCE_FAILURES[key.value if isinstance(key, InventoryState) else key]
    return status, {"error": message, "code": code}
