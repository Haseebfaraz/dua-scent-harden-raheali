from app.fragrance.scoring import SCORE_WEIGHTS, classify_dislike_conflict, compute_evidence_level, matched_likes


def test_score_weights_match_spec_exact_values():
    assert SCORE_WEIGHTS["sameCity"] == 5
    assert SCORE_WEIGHTS["sameCountry"] == 4
    assert SCORE_WEIGHTS["sameStateRegionOrClimate"] == 3
    assert SCORE_WEIGHTS["sameSeason"] == 4
    assert SCORE_WEIGHTS["matchesLike"] == 5
    assert SCORE_WEIGHTS["conflictsDislike"] == -10
    assert SCORE_WEIGHTS["repeatPurchaseBySimilarCustomer"] == 3
    assert SCORE_WEIGHTS["popularAmongSimilarCustomers"] == 2


def test_classify_dislike_conflict_none_when_absent():
    result = classify_dislike_conflict(["Bergamot", "Musk"], ["spicy"])
    assert result == {"severity": "none", "matchedFamilies": [], "matchedNoteCount": 0}


def test_classify_dislike_conflict_low_for_single_non_prominent_note():
    notes = ["Vanilla", "Sugar", "Marshmallow", "Honey", "Tonka", "Oud"]
    result = classify_dislike_conflict(notes, ["strongHeavy"])
    assert result["severity"] == "low"
    assert result["matchedFamilies"] == ["strongHeavy"]


def test_classify_dislike_conflict_medium_for_prominent_single_note():
    notes = ["Oud", "Sugar", "Marshmallow"]
    result = classify_dislike_conflict(notes, ["strongHeavy"])
    assert result["severity"] == "medium"


def test_classify_dislike_conflict_high_for_multiple_conflicting_notes():
    notes = ["Oud", "Leather", "Tobacco", "Vanilla"]
    result = classify_dislike_conflict(notes, ["strongHeavy"])
    assert result["severity"] == "high"


def test_classify_dislike_conflict_does_not_auto_reject_minor_supporting_note():
    notes = ["Vanilla", "Sugar", "Marshmallow", "Honey", "Tonka", "Musk", "Amber"]
    result = classify_dislike_conflict(notes, ["strongHeavy"])
    assert result["severity"] != "high"


def test_matched_likes_only_genuinely_present_families():
    notes = ["Mango", "Vanilla", "Bergamot"]
    assert sorted(matched_likes(notes, ["fruity", "sweet", "spicy"])) == ["fruity", "sweet"]


def test_matched_likes_empty_when_nothing_matches():
    assert matched_likes(["Oud", "Leather"], ["fruity", "sweet"]) == []


def test_compute_evidence_level_high():
    assert compute_evidence_level(distinct_similar_customers=12, same_season_orders=0) == "high"
    assert compute_evidence_level(distinct_similar_customers=0, same_season_orders=30) == "high"


def test_compute_evidence_level_medium():
    assert compute_evidence_level(distinct_similar_customers=4, same_season_orders=0) == "medium"


def test_compute_evidence_level_low():
    assert compute_evidence_level() == "low"
    assert compute_evidence_level(distinct_similar_customers=1, same_season_orders=2) == "low"
