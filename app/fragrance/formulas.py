"""Port of app/services/fragranceFormula.server.js -- Odoo oil-inventory manufacturing formula.
Separate from, and never touching, the Top/Middle/Base display math or PricePer5ml pricing.

A 34ml bottle is oilTotalMl (12-14ml, default 13ml) of real fragrance oil plus alcohol as the
remainder. The recommendation's ratioPercent applies ONLY to the oil portion -- never to the
full 34ml.
"""

import math
from typing import Any

FINISHED_BOTTLE_ML = 34
DEFAULT_OIL_ML = 13
MIN_OIL_ML = 12
MAX_OIL_ML = 14

_RATIO_SUM_TOLERANCE = 0.5  # percentage points


def compute_alcohol_ml(oil_total_ml: float = DEFAULT_OIL_ML) -> float:
    return FINISHED_BOTTLE_ML - oil_total_ml


def compute_required_oil_ml(oil_total_ml: float, ratio_percent: float) -> float:
    """Rounded to 0.01ml -- finer than that is floating-point noise, not real manufacturing
    precision; nothing about this bottle is measured to the micro-liter.
    """
    return round(oil_total_ml * (ratio_percent / 100) * 100) / 100


def build_production_formula(ratios: list[dict[str, Any]], oil_total_ml: float = DEFAULT_OIL_ML) -> dict[str, Any]:
    """The single source of truth for both the feasibility check and the recommendation-inventory
    snapshot. ratios: [{"productTitle": str, "ratioPercent": float, "fragranceProductId"?: str}].
    """
    if oil_total_ml < MIN_OIL_ML or oil_total_ml > MAX_OIL_ML or oil_total_ml != oil_total_ml:  # NaN check
        raise ValueError(f"oilTotalMl must be between {MIN_OIL_ML} and {MAX_OIL_ML} (got {oil_total_ml}).")
    if not ratios:
        raise ValueError("A production formula requires at least one ratio component.")

    pct_sum = sum(r.get("ratioPercent") or 0 for r in ratios)
    if any(not (r.get("ratioPercent", float("nan")) >= 0) for r in ratios):
        raise ValueError("Ratio percentages must be non-negative numbers.")
    if abs(pct_sum - 100) > _RATIO_SUM_TOLERANCE:
        raise ValueError(f"Ratio percentages must total 100% within tolerance (got {pct_sum}%).")

    alcohol_ml = compute_alcohol_ml(oil_total_ml)
    components = [
        {
            "fragranceProductId": r.get("fragranceProductId"),
            "productTitle": r["productTitle"],
            "ratioPercent": r["ratioPercent"],
            "requiredOilMl": compute_required_oil_ml(oil_total_ml, r["ratioPercent"]),
        }
        for r in ratios
    ]

    oil_sum = sum(c["requiredOilMl"] for c in components)
    if abs(oil_sum - oil_total_ml) > _RATIO_SUM_TOLERANCE * (oil_total_ml / 100):
        raise ValueError(f"Component oil quantities must total oilTotalMl (got {oil_sum} vs {oil_total_ml}).")
    if abs(oil_total_ml + alcohol_ml - FINISHED_BOTTLE_ML) > 0.001:
        raise ValueError(f"oilTotalMl + alcoholMl must equal {FINISHED_BOTTLE_ML}ml.")

    return {"bottleSizeMl": FINISHED_BOTTLE_ML, "oilTotalMl": oil_total_ml, "alcoholMl": alcohol_ml, "components": components}


def compute_component_capacity(available_oil_ml: float | None, required_oil_ml_per_bottle: float) -> int:
    """How many whole bottles this single component's available oil could supply."""
    if not (required_oil_ml_per_bottle > 0) or available_oil_ml is None or not (available_oil_ml >= 0):
        return 0
    return math.floor(available_oil_ml / required_oil_ml_per_bottle)


def compute_feasibility(components: list[dict[str, Any]]) -> dict[str, Any]:
    """A combination is buildable only if every component has enough available oil for at least
    one bottle; the true maximum is the smallest per-component capacity (the limiting oil).
    components: [{"productTitle": str, "requiredOilMl": float, "availableOilMl": float|None}].
    """
    per_component = []
    for c in components:
        available = c.get("availableOilMl")
        capacity = 0 if available is None else compute_component_capacity(available, c["requiredOilMl"])
        per_component.append({
            "productTitle": c["productTitle"],
            "requiredOilMl": c["requiredOilMl"],
            "availableOilMl": available,
            "capacity": capacity,
            "buildableForComponent": available is not None and available >= c["requiredOilMl"],
        })

    buildable = all(c["buildableForComponent"] for c in per_component)
    maximum_buildable_bottles = min((c["capacity"] for c in per_component)) if buildable else 0
    limiting = None
    for c in per_component:
        if limiting is None or c["capacity"] < limiting["capacity"]:
            limiting = c

    return {
        "buildable": buildable,
        "maximumBuildableBottles": maximum_buildable_bottles,
        "limitingProductTitle": limiting["productTitle"] if limiting else None,
        "components": per_component,
    }
