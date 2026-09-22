import pytest

from app.fragrance.formulas import (
    DEFAULT_OIL_ML,
    FINISHED_BOTTLE_ML,
    MAX_OIL_ML,
    MIN_OIL_ML,
    build_production_formula,
    compute_alcohol_ml,
    compute_component_capacity,
    compute_feasibility,
)


def test_compute_alcohol_ml_13ml_oil():
    assert compute_alcohol_ml(13) == 21


def test_compute_alcohol_ml_min():
    assert compute_alcohol_ml(MIN_OIL_ML) == 22


def test_compute_alcohol_ml_max():
    assert compute_alcohol_ml(MAX_OIL_ML) == 20


def test_compute_alcohol_ml_default():
    assert compute_alcohol_ml() == FINISHED_BOTTLE_ML - DEFAULT_OIL_ML


def test_hybrid_65_35():
    formula = build_production_formula([
        {"productTitle": "Herbs & Sea Salt", "ratioPercent": 65},
        {"productTitle": "Cali Life", "ratioPercent": 35},
    ])
    assert formula["oilTotalMl"] == 13
    assert formula["alcoholMl"] == 21
    assert [c["requiredOilMl"] for c in formula["components"]] == [8.45, 4.55]


def test_hybrid_60_40():
    formula = build_production_formula([{"productTitle": "A", "ratioPercent": 60}, {"productTitle": "B", "ratioPercent": 40}])
    assert [c["requiredOilMl"] for c in formula["components"]] == [7.8, 5.2]


def test_tribrid_50_30_20():
    formula = build_production_formula([
        {"productTitle": "A", "ratioPercent": 50}, {"productTitle": "B", "ratioPercent": 30}, {"productTitle": "C", "ratioPercent": 20},
    ])
    assert [c["requiredOilMl"] for c in formula["components"]] == [6.5, 3.9, 2.6]


def test_quadbrid_40_30_20_10():
    formula = build_production_formula([
        {"productTitle": "A", "ratioPercent": 40}, {"productTitle": "B", "ratioPercent": 30},
        {"productTitle": "C", "ratioPercent": 20}, {"productTitle": "D", "ratioPercent": 10},
    ])
    assert [c["requiredOilMl"] for c in formula["components"]] == [5.2, 3.9, 2.6, 1.3]


def test_rejects_ratios_not_totaling_100():
    with pytest.raises(ValueError, match="total 100"):
        build_production_formula([{"productTitle": "A", "ratioPercent": 60}, {"productTitle": "B", "ratioPercent": 30}])


def test_rejects_negative_ratio():
    with pytest.raises(ValueError, match="non-negative"):
        build_production_formula([{"productTitle": "A", "ratioPercent": -10}, {"productTitle": "B", "ratioPercent": 110}])


def test_rejects_nan_ratio():
    with pytest.raises(ValueError, match="non-negative"):
        build_production_formula([{"productTitle": "A", "ratioPercent": float("nan")}, {"productTitle": "B", "ratioPercent": 100}])


def test_rejects_empty_formula():
    with pytest.raises(ValueError, match="at least one"):
        build_production_formula([])


def test_rejects_oil_total_out_of_range():
    with pytest.raises(ValueError, match="between 12 and 14"):
        build_production_formula([{"productTitle": "A", "ratioPercent": 100}], 16)


def test_feasibility_all_available_buildable():
    result = compute_feasibility([
        {"productTitle": "Herbs & Sea Salt", "requiredOilMl": 8.45, "availableOilMl": 500},
        {"productTitle": "Cali Life", "requiredOilMl": 4.55, "availableOilMl": 100},
    ])
    assert result["buildable"] is True
    assert result["maximumBuildableBottles"] == 21
    assert result["limitingProductTitle"] == "Cali Life"


def test_feasibility_insufficient_not_buildable():
    result = compute_feasibility([
        {"productTitle": "Herbs & Sea Salt", "requiredOilMl": 8.45, "availableOilMl": 500},
        {"productTitle": "Cali Life", "requiredOilMl": 4.55, "availableOilMl": 3},
    ])
    assert result["buildable"] is False
    assert result["maximumBuildableBottles"] == 0


def test_feasibility_null_availability_is_zero_capacity():
    result = compute_feasibility([{"productTitle": "A", "requiredOilMl": 5, "availableOilMl": None}])
    assert result["buildable"] is False
    assert result["components"][0]["capacity"] == 0


def test_compute_component_capacity_floors():
    assert compute_component_capacity(100, 4.55) == 21
    assert compute_component_capacity(0, 4.55) == 0
    assert compute_component_capacity(100, 0) == 0
