from app.ai.tool_executor import evaluate_auto_confirm_eligibility
from app.services.recommendation_engine import CUSTOMER_FIT_LOW_THRESHOLD


def _candidate(**overrides):
    base = {
        "type": "HYBRID",
        "internalProducts": [
            {"title": "A", "notes": ["Bergamot", "Lemon"], "contribution": "Freshness"},
            {"title": "B", "notes": ["Vanilla", "Musk"], "contribution": "Sweetness"},
        ],
        "recommendedRatio": [
            {"productTitle": "A", "ratioPercent": 50},
            {"productTitle": "B", "ratioPercent": 50},
        ],
        "confidenceBreakdown": {
            "customerFit": {"value": "high"}, "compatibility": {"value": "high"},
            "historical": {"value": "low"}, "data": {"value": "low"}, "novelty": {"value": "low"},
        },
        "riskBreakdown": [],
    }
    base.update(overrides)
    return base


def test_auto_confirms_strong_fit_zero_history():
    c = _candidate(confidenceBreakdown={"customerFit": {"value": "high"}, "compatibility": {"value": "high"}, "historical": {"value": "low"}})
    result = evaluate_auto_confirm_eligibility(c, {"dislikes": []})
    assert result["autoConfirmEligible"] is True
    assert result["autoConfirmReasons"] == []


def test_blocks_weak_customer_fit_despite_excellent_history():
    c = _candidate(confidenceBreakdown={"customerFit": {"value": "low"}, "compatibility": {"value": "high"}, "historical": {"value": "high"}})
    result = evaluate_auto_confirm_eligibility(c, {"dislikes": []})
    assert result["autoConfirmEligible"] is False
    assert "customer_fit_low" in result["autoConfirmReasons"]


def test_blocks_poor_compatibility():
    c = _candidate(confidenceBreakdown={"customerFit": {"value": "high"}, "compatibility": {"value": "low"}})
    result = evaluate_auto_confirm_eligibility(c, {"dislikes": []})
    assert result["autoConfirmEligible"] is False
    assert "compatibility_low" in result["autoConfirmReasons"]


def test_advisory_low_risks_do_not_block():
    c = _candidate(
        confidenceBreakdown={"customerFit": {"value": "high"}, "compatibility": {"value": "medium"}},
        riskBreakdown=[{"counted": True, "severity": "advisory"}, {"counted": True, "severity": "low"}],
    )
    result = evaluate_auto_confirm_eligibility(c, {"dislikes": []})
    assert result["autoConfirmEligible"] is True
    assert result["highestCountedRiskSeverity"] == "low"


def test_high_and_critical_risks_block():
    high = _candidate(riskBreakdown=[{"counted": True, "severity": "high"}])
    high_result = evaluate_auto_confirm_eligibility(high, {"dislikes": []})
    assert high_result["autoConfirmEligible"] is False
    assert "high_severity_risk:high" in high_result["autoConfirmReasons"]

    critical = _candidate(riskBreakdown=[{"counted": True, "severity": "critical"}])
    critical_result = evaluate_auto_confirm_eligibility(critical, {"dislikes": []})
    assert critical_result["autoConfirmEligible"] is False
    assert "high_severity_risk:critical" in critical_result["autoConfirmReasons"]


def test_duplicate_direction_medium_does_not_block_but_unrelated_high_does():
    medium_only = _candidate(riskBreakdown=[{"counted": True, "severity": "medium", "id": "duplicate_direction"}])
    assert evaluate_auto_confirm_eligibility(medium_only, {"dislikes": []})["autoConfirmEligible"] is True

    unrelated_high = _candidate(riskBreakdown=[{"counted": True, "severity": "high", "id": "excessive_direction_stacking"}])
    assert evaluate_auto_confirm_eligibility(unrelated_high, {"dislikes": []})["autoConfirmEligible"] is False


def test_ignores_deduplicated_out_high_severity_risk():
    c = _candidate(riskBreakdown=[{"counted": False, "severity": "high"}])
    result = evaluate_auto_confirm_eligibility(c, {"dislikes": []})
    assert result["autoConfirmEligible"] is True
    assert result["highestCountedRiskSeverity"] is None


def test_hard_dislike_always_blocks():
    c = _candidate(
        internalProducts=[
            {"title": "A", "notes": ["Sandalwood", "Bergamot"], "contribution": "Base"},
            {"title": "B", "notes": ["Vanilla"], "contribution": "Sweetness"},
        ],
        confidenceBreakdown={"customerFit": {"value": "high"}, "compatibility": {"value": "high"}, "historical": {"value": "high"}},
        riskBreakdown=[],
    )
    result = evaluate_auto_confirm_eligibility(c, {"dislikes": ["Sandalwood"]})
    assert result["autoConfirmEligible"] is False
    assert "hard_dislike_conflict" in result["autoConfirmReasons"]
    assert result["hasHardDislikeConflict"] is True


def test_family_dislike_not_literal_note_does_not_trip_hard_check():
    c = _candidate(confidenceBreakdown={"customerFit": {"value": "high"}, "compatibility": {"value": "high"}})
    result = evaluate_auto_confirm_eligibility(c, {"dislikes": ["Woody fragrances"]})
    assert result["hasHardDislikeConflict"] is False


def test_blocks_invalid_shape():
    c = _candidate(
        recommendedRatio=[{"productTitle": "A", "ratioPercent": 40}, {"productTitle": "B", "ratioPercent": 40}],
        confidenceBreakdown={"customerFit": {"value": "high"}, "compatibility": {"value": "high"}},
        riskBreakdown=[],
    )
    result = evaluate_auto_confirm_eligibility(c, {"dislikes": []})
    assert result["autoConfirmEligible"] is False
    assert "invalid_shape" in result["autoConfirmReasons"]
    assert result["shapeValid"] is False


def test_exposes_customer_fit_threshold():
    result = evaluate_auto_confirm_eligibility(_candidate(), {"dislikes": []})
    assert result["customerFitThreshold"] == CUSTOMER_FIT_LOW_THRESHOLD


def test_reports_highest_severity_not_first():
    c = _candidate(
        confidenceBreakdown={"customerFit": {"value": "high"}, "compatibility": {"value": "medium"}},
        riskBreakdown=[{"counted": True, "severity": "advisory"}, {"counted": True, "severity": "medium"}, {"counted": True, "severity": "low"}],
    )
    result = evaluate_auto_confirm_eligibility(c, {"dislikes": []})
    assert result["highestCountedRiskSeverity"] == "medium"
    assert result["autoConfirmEligible"] is True


def test_blocks_when_no_stated_preference_coverage():
    c = _candidate(
        confidenceBreakdown={"customerFit": {"value": "high"}, "compatibility": {"value": "high"}},
        riskBreakdown=[], requestedPreferenceFamilies=["floral"], matchedPreferenceFamilies=[],
    )
    result = evaluate_auto_confirm_eligibility(c, {"dislikes": []})
    assert result["autoConfirmEligible"] is False
    assert "no_stated_preference_coverage" in result["autoConfirmReasons"]


def test_does_not_block_partial_multi_family_coverage():
    c = _candidate(
        confidenceBreakdown={"customerFit": {"value": "high"}, "compatibility": {"value": "high"}},
        riskBreakdown=[], requestedPreferenceFamilies=["fresh", "floral"], matchedPreferenceFamilies=["fresh"],
    )
    result = evaluate_auto_confirm_eligibility(c, {"dislikes": []})
    assert "no_stated_preference_coverage" not in result["autoConfirmReasons"]


def test_never_fires_with_no_recognized_liked_families():
    c = _candidate(
        confidenceBreakdown={"customerFit": {"value": "high"}, "compatibility": {"value": "high"}},
        riskBreakdown=[], requestedPreferenceFamilies=[], matchedPreferenceFamilies=[],
    )
    result = evaluate_auto_confirm_eligibility(c, {"dislikes": []})
    assert "no_stated_preference_coverage" not in result["autoConfirmReasons"]


def test_legacy_candidate_with_no_preference_family_fields_is_never_a_false_failure():
    c = _candidate(confidenceBreakdown={"customerFit": {"value": "high"}, "compatibility": {"value": "high"}}, riskBreakdown=[])
    c.pop("requestedPreferenceFamilies", None)
    c.pop("matchedPreferenceFamilies", None)
    result = evaluate_auto_confirm_eligibility(c, {"dislikes": []})
    assert result["autoConfirmEligible"] is True
    assert "no_stated_preference_coverage" not in result["autoConfirmReasons"]
