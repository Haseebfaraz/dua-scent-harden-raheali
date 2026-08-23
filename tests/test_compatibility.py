import pytest

from app.fragrance.compatibility import (
    COMPATIBILITY_TAGS,
    PREFERENCE_FAMILIES,
    assess_combination_risk_details,
    assess_combination_risks,
    classify_almond_character,
    count_avoided_direction_matches,
    count_preferred_direction_matches,
    detect_families,
    detect_intensity_drivers,
    exact_note_coverage_score,
    family_breadth_coverage_score,
    group_and_penalize_risks,
    interpret_customer_preferences,
    interpret_lifestyle_context,
    literal_note_match_count,
    literal_note_terms_from_likes,
    matched_literal_terms,
    matched_real_notes_in_text,
    missing_literal_terms,
    pair_is_compatible,
    passes_intensity_filter,
    compute_complexity_level,
    split_dislikes_by_exactness,
    text_to_preference_families,
)
from app.fragrance.scoring import classify_dislike_conflict, matched_likes, like_match_strength


def test_detect_families_real_notes():
    assert "fruity" in detect_families(["Mango", "Pineapple"], PREFERENCE_FAMILIES)
    assert "sweet" in detect_families(["Vanilla", "Sugar"], PREFERENCE_FAMILIES)
    assert "fresh" in detect_families(["Bergamot", "Lemon"], PREFERENCE_FAMILIES)
    assert "spicy" in detect_families(["Saffron", "Cinnamon"], PREFERENCE_FAMILIES)
    assert "strongHeavy" in detect_families(["Oud", "Leather"], PREFERENCE_FAMILIES)


def test_detect_families_catch_all_notes():
    assert "fruity" in detect_families(["Fruity Notes"], PREFERENCE_FAMILIES)
    assert "spicy" in detect_families(["Spices"], PREFERENCE_FAMILIES)


def test_detect_families_empty_for_no_match():
    assert detect_families(["Zzznotarealnoteatall"], PREFERENCE_FAMILIES) == []
    assert detect_families([], PREFERENCE_FAMILIES) == []
    assert detect_families(None, PREFERENCE_FAMILIES) == []


def test_text_to_preference_families_acceptance_words():
    assert text_to_preference_families(["Fruity"]) == ["fruity"]
    assert text_to_preference_families(["Sweet"]) == ["sweet"]
    assert text_to_preference_families(["Spicy"]) == ["spicy"]
    assert text_to_preference_families(["Strong"]) == ["strongHeavy"]


def test_text_to_preference_families_bare_candy():
    assert text_to_preference_families(["candy"]) == ["sweet"]
    assert text_to_preference_families(["candy want some candy type also.."]) == ["sweet"]


def test_text_to_preference_families_coconut():
    assert text_to_preference_families(["coconut"]) == ["fruity"]
    assert text_to_preference_families(["dont want coconut"]) == ["fruity"]


def test_literal_note_terms_from_likes_extracts_specific_notes():
    terms = literal_note_terms_from_likes(["Fruity", "Fresh", "Apple", "Strawberry", "Peach"])
    assert {"apple", "strawberry", "peach"} <= set(terms)
    assert "fruity" not in terms
    assert "fresh" not in terms


def test_literal_note_terms_from_likes_coconut():
    assert "coconut" in literal_note_terms_from_likes(["dont want coconut"])


def test_literal_note_match_count_zero_for_family_only_match():
    terms = literal_note_terms_from_likes(["Apple", "Strawberry", "Peach"])
    assert literal_note_match_count(["Pear", "Blackcurrant", "Musk"], terms) == 0


def test_literal_note_match_count_real_matches():
    terms = literal_note_terms_from_likes(["Apple", "Strawberry", "Peach"])
    assert literal_note_match_count(["Peach", "Musk", "Vanilla"], terms) == 1
    assert literal_note_match_count(["Apple", "Peach", "Vanilla"], terms) == 2


def test_literal_note_match_count_pineapple_apple_word_boundary():
    terms = literal_note_terms_from_likes(["Apple"])
    assert literal_note_match_count(["Pineapple Slice"], terms) == 0
    assert literal_note_match_count(["Pink Lady Apple"], terms) == 1


def test_literal_note_terms_pineapple_does_not_leak_apple():
    terms = literal_note_terms_from_likes(["Pineapple"])
    assert "pineapple" in terms
    assert "apple" not in terms
    assert literal_note_match_count(["Pink Lady Apple"], terms) == 0


def test_literal_note_match_count_pear_peach_never_cross_match():
    peach_terms = literal_note_terms_from_likes(["Peach"])
    assert literal_note_match_count(["Pear"], peach_terms) == 0
    pear_terms = literal_note_terms_from_likes(["Pear"])
    assert literal_note_match_count(["Peach"], pear_terms) == 0


def test_literal_note_terms_berry_catch_all_does_not_match_strawberry():
    terms = literal_note_terms_from_likes(["Strawberry"])
    assert "berr" not in terms
    assert literal_note_match_count(["Raspberry", "Blackberry", "Blueberry"], terms) == 0
    assert literal_note_match_count(["Strawberry Purée"], terms) == 1


def test_literal_note_terms_wood_does_not_match_sandalwood():
    terms = literal_note_terms_from_likes(["Wood"])
    assert literal_note_match_count(["Sandalwood"], terms) == 0
    assert literal_note_match_count(["Aged Wood Accord"], terms) == 1


def test_matched_real_notes_in_text_recognizes_notes_outside_family_vocab():
    assert text_to_preference_families(["Jackfruit"]) == []
    assert matched_real_notes_in_text("dont want jackfruit", ["Gin", "Mojito", "Jackfruit"]) == ["jackfruit"]


def test_matched_real_notes_in_text_only_matches_given_list():
    assert matched_real_notes_in_text("dont want jackfruit", ["Gin", "Mojito", "Coconut"]) == []


def test_matched_real_notes_in_text_multiword_phrase():
    assert matched_real_notes_in_text("remove the griotte syrup please", ["Black Cherry", "Griotte Syrup"]) == ["griotte syrup"]


def test_matched_real_notes_in_text_no_false_substring_match():
    assert matched_real_notes_in_text("pineapple please", ["Apple"]) == []


def test_matched_real_notes_in_text_empty_input():
    assert matched_real_notes_in_text("", ["Coconut"]) == []
    assert matched_real_notes_in_text("dont want coconut", []) == []
    assert matched_real_notes_in_text("dont want coconut", None) == []


def test_exact_note_coverage_score_zero():
    assert exact_note_coverage_score(0) == 0


def test_exact_note_coverage_score_first_three_tiers():
    assert exact_note_coverage_score(1) == 10
    assert exact_note_coverage_score(2) == 17
    assert exact_note_coverage_score(3) == 22


def test_exact_note_coverage_score_floors_beyond_third():
    assert exact_note_coverage_score(4) == 27
    assert exact_note_coverage_score(5) == 32


def test_exact_note_coverage_score_beats_flat_family_bonus():
    assert exact_note_coverage_score(1) > 5


def test_matched_and_missing_literal_terms():
    terms = literal_note_terms_from_likes(["Apple", "Strawberry", "Peach"])
    notes = ["Peach Purée", "Musk", "Vanilla"]
    assert matched_literal_terms(notes, terms) == ["peach"]
    missing = missing_literal_terms(notes, terms)
    assert "apple" in missing and "strawberry" in missing
    assert "peach" not in missing


def test_missing_literal_terms_everything_missing():
    terms = literal_note_terms_from_likes(["Apple", "Strawberry"])
    assert matched_literal_terms(["Musk", "Cedar"], terms) == []
    missing = missing_literal_terms(["Musk", "Cedar"], terms)
    assert "apple" in missing and "strawberry" in missing


def test_missing_literal_terms_nothing_missing():
    terms = literal_note_terms_from_likes(["Apple", "Peach"])
    notes = ["Apple Sauce", "Peach Purée"]
    assert missing_literal_terms(notes, terms) == []
    assert len(matched_literal_terms(notes, terms)) == 2


def test_pair_is_compatible_confirms_listed_pairs():
    assert pair_is_compatible("fruity", "citrus") is True
    assert pair_is_compatible("citrus", "fruity") is True
    assert pair_is_compatible("smoky", "amber") is True
    assert pair_is_compatible("woody", "amber") is True


def test_pair_is_compatible_rejects_unlisted_pairs():
    assert pair_is_compatible("spicy", "musk") is False
    assert pair_is_compatible("strongHeavy", "fruity") is False


def test_assess_combination_risks_gourmand_summer_only():
    products = [
        {"title": "A", "notes": ["Sugar", "Caramel"]},
        {"title": "B", "notes": ["Marshmallow", "Honey"]},
    ]
    assert any("summer heat" in r for r in assess_combination_risks(products, {"season": "Summer"}))
    assert not any("summer heat" in r for r in assess_combination_risks(products, {"season": "Winter"}))


def test_assess_combination_risks_multiple_heavy_components():
    products = [
        {"title": "A", "notes": ["Oud", "Leather"]},
        {"title": "B", "notes": ["Tobacco", "Resin"]},
    ]
    assert any("heavy oud/leather" in r for r in assess_combination_risks(products))


def test_assess_combination_risks_competing_fruits():
    products = [
        {"title": "A", "notes": ["Mango"]},
        {"title": "B", "notes": ["Pineapple"]},
        {"title": "C", "notes": ["Peach"]},
    ]
    assert any("fruity products may compete" in r for r in assess_combination_risks(products))


def test_assess_combination_risks_quadbrid_complexity():
    products = [
        {"title": "A", "notes": ["Vanilla"]},
        {"title": "B", "notes": ["Musk"]},
        {"title": "C", "notes": ["Cedar"]},
        {"title": "D", "notes": ["Amber"]},
    ]
    assert any("Four products" in r for r in assess_combination_risks(products))


def test_assess_combination_risks_duplicate_direction():
    products = [{"title": "A", "notes": ["Mango"]}, {"title": "B", "notes": ["Pineapple"]}]
    assert any("no contrasting role" in r for r in assess_combination_risks(products))


def test_assess_combination_risks_balanced_combination_has_none():
    products = [
        {"title": "A", "notes": ["Bergamot", "Lemon"]},
        {"title": "B", "notes": ["Musk", "Cedar"]},
    ]
    assert assess_combination_risks(products, {"season": "Winter"}) == []


def test_risk_details_weights_single_advisory_lighter_than_flat_ten():
    products = [
        {"title": "A", "notes": ["Bergamot"]},
        {"title": "B", "notes": ["Vanilla"]},
        {"title": "C", "notes": ["Cedar"]},
        {"title": "D", "notes": ["Musk"]},
    ]
    result = assess_combination_risk_details(products)
    assert len(result["breakdown"]) == 1
    assert result["breakdown"][0]["id"] == "quadbrid_complexity"
    assert result["breakdown"][0]["severity"] == "advisory"
    assert result["breakdown"][0]["counted"] is True
    assert result["breakdown"][0]["penalty"] == -1
    assert result["riskPenalty"] == -1
    assert result["hasCritical"] is False


def test_risk_details_weights_duplicate_direction_medium():
    products = [{"title": "A", "notes": ["Mango"]}, {"title": "B", "notes": ["Pineapple"]}]
    result = assess_combination_risk_details(products)
    assert len(result["breakdown"]) == 1
    hit = result["breakdown"][0]
    assert hit["id"] == "duplicate_direction"
    assert hit["severity"] == "medium"
    assert hit["counted"] is True
    assert hit["penalty"] == -5
    assert result["riskPenalty"] == -5


def test_risk_details_groups_competing_fruits_and_duplicate_direction():
    products = [
        {"title": "A", "notes": ["Mango"]},
        {"title": "B", "notes": ["Pineapple"]},
        {"title": "C", "notes": ["Guava"]},
    ]
    result = assess_combination_risk_details(products)
    assert len(result["breakdown"]) == 2
    duplicate = next(r for r in result["breakdown"] if r["id"] == "duplicate_direction")
    competing = next(r for r in result["breakdown"] if r["id"] == "competing_fruits")
    assert duplicate["severity"] == "medium" and duplicate["counted"] is True and duplicate["penalty"] == -5
    assert competing["severity"] == "low" and competing["counted"] is False and competing["penalty"] == 0
    assert result["riskPenalty"] == -5


def test_single_family_concentration_stays_advisory_with_role_diversity():
    products = [
        {"title": "A", "notes": ["Mango", "Bergamot"]},
        {"title": "B", "notes": ["Pineapple", "Vanilla"]},
    ]
    roled_products = [
        {**products[0], "role": "Main fruit body"},
        {**products[1], "role": "Sweetness"},
    ]
    result = assess_combination_risk_details(products, {"likeFamilies": ["fruity"], "roledProducts": roled_products})
    assert not any(r["id"] == "duplicate_direction" for r in result["breakdown"])
    hit = next(r for r in result["breakdown"] if r["id"] == "single_family_concentration")
    assert hit["severity"] == "advisory"
    assert hit["counted"] is True
    assert hit["penalty"] == -1


def test_single_family_concentration_escalates_to_high_hot_weather():
    products = [
        {"title": "A", "notes": ["Mango", "Sugar"]},
        {"title": "B", "notes": ["Pineapple", "Caramel"]},
        {"title": "C", "notes": ["Guava", "Marshmallow"]},
    ]
    result = assess_combination_risk_details(products, {"likeFamilies": ["fruity"], "season": "Summer"})
    hit = next(r for r in result["breakdown"] if r["id"] == "excessive_direction_stacking")
    assert hit["severity"] == "high"
    assert hit["counted"] is True
    assert hit["penalty"] == -10


def test_single_family_concentration_dry_earthy_advisory():
    products = [
        {"title": "A", "notes": ["Vetiver", "Bergamot"]},
        {"title": "B", "notes": ["Moss", "Vanilla"]},
    ]
    roled_products = [
        {**products[0], "role": "Freshness"},
        {**products[1], "role": "Sweetness"},
    ]
    result = assess_combination_risk_details(products, {"likeFamilies": ["dry", "earthy"], "roledProducts": roled_products})
    hit = next(r for r in result["breakdown"] if r["id"] == "single_family_concentration")
    assert hit["severity"] == "advisory"


def test_single_family_concentration_escalates_medium_high_near_duplicate():
    products = [
        {"title": "A", "notes": ["Mango", "Pineapple", "Guava", "Bergamot"]},
        {"title": "B", "notes": ["Mango", "Pineapple", "Guava", "Vanilla"]},
    ]
    result = assess_combination_risk_details(products, {"likeFamilies": ["fruity"]})
    hit = next(r for r in result["breakdown"] if r["id"] == "excessive_direction_stacking")
    assert hit is not None
    assert hit["severity"] in ("medium", "high")


def test_duplicate_direction_not_requested_stays_medium_not_exempted():
    products = [{"title": "A", "notes": ["Mango"]}, {"title": "B", "notes": ["Pineapple"]}]
    result = assess_combination_risk_details(products, {"likeFamilies": ["woody"]})
    hit = next(r for r in result["breakdown"] if r["id"] == "duplicate_direction")
    assert hit["severity"] == "medium"
    assert hit["counted"] is True
    assert hit["penalty"] == -5
    assert not any(r["id"] == "single_family_concentration" for r in result["breakdown"])


_FRESH_PRODUCTS = [
    {"title": "A", "notes": ["Bergamot", "Citrus"]},
    {"title": "B", "notes": ["Lemon", "Mint"]},
]


def test_duplicate_direction_fully_exempted_when_liked():
    result = assess_combination_risk_details(_FRESH_PRODUCTS, {"likeFamilies": text_to_preference_families(["fresh"])})
    assert not any(r["id"] == "duplicate_direction" for r in result["breakdown"])


def test_duplicate_direction_fires_medium_when_not_liked():
    result = assess_combination_risk_details(_FRESH_PRODUCTS, {"likeFamilies": []})
    hit = next(r for r in result["breakdown"] if r["id"] == "duplicate_direction")
    assert hit["severity"] == "medium"


def test_dislike_pipeline_independent_of_duplicate_direction():
    split = split_dislikes_by_exactness(["fresh"])
    assert "fresh" in split["explicitFamilyDislikes"]
    conflict_a = classify_dislike_conflict(_FRESH_PRODUCTS[0]["notes"], split["explicitFamilyDislikes"])
    conflict_b = classify_dislike_conflict(_FRESH_PRODUCTS[1]["notes"], split["explicitFamilyDislikes"])
    assert conflict_a["severity"] != "none"
    assert conflict_b["severity"] != "none"


def test_duplicate_direction_unrelated_like_stays_medium():
    result = assess_combination_risk_details(_FRESH_PRODUCTS, {"likeFamilies": text_to_preference_families(["woody"])})
    hit = next(r for r in result["breakdown"] if r["id"] == "duplicate_direction")
    assert hit["severity"] == "medium"


def test_group_and_penalize_risks_uncorrelated():
    result = group_and_penalize_risks([
        {"id": "a", "message": "a", "severity": "advisory"},
        {"id": "b", "message": "b", "severity": "low"},
    ])
    assert result["riskPenalty"] == -3
    assert all(r["counted"] for r in result["breakdown"])


def test_group_and_penalize_risks_correlated_only_highest_counts():
    result = group_and_penalize_risks(
        [
            {"id": "a", "message": "a", "severity": "low"},
            {"id": "b", "message": "b", "severity": "high"},
        ],
        lambda hit: "same-group",
    )
    assert result["riskPenalty"] == -10
    a = next(r for r in result["breakdown"] if r["id"] == "a")
    b = next(r for r in result["breakdown"] if r["id"] == "b")
    assert a["counted"] is False and a["penalty"] == 0
    assert b["counted"] is True and b["penalty"] == -10


def test_group_and_penalize_risks_has_critical():
    result = group_and_penalize_risks([{"id": "x", "message": "x", "severity": "critical"}])
    assert result["hasCritical"] is True


def test_group_and_penalize_risks_no_critical():
    result = group_and_penalize_risks([{"id": "x", "message": "x", "severity": "high"}])
    assert result["hasCritical"] is False


def test_interpret_customer_preferences_aniq_complaint():
    profile = {
        "dislikes": ["I do not like scents that hit my nose and make me feel headache."],
        "additionalPreferences": ["I do not like scents that hit my nose and make me feel headache."],
        "preferredStyle": "Relaxing",
    }
    result = interpret_customer_preferences(profile)
    assert result["sensitivityLevel"] == "high"
    assert result["strengthPreference"] == "light"
    assert result["preferSimpleCombinations"] is True
    assert result["preferredCombinationTypes"] == ["HYBRID"]
    for direction in ["relaxing", "airy", "clean", "watery", "green-tea", "soft-musky", "light-fruity"]:
        assert direction in result["preferredDirections"]
    for direction in ["sharp", "piercing", "pepper-heavy", "dense-spicy", "smoky", "heavy-amber", "oud", "leather", "tobacco"]:
        assert direction in result["avoidedDirections"]


@pytest.mark.parametrize("dislike", ["it feels too haddik on me", "gives me a headache", "it's overpowering and suffocating", "I cannot tolerate strong perfume"])
def test_interpret_customer_preferences_informal_phrasing(dislike):
    assert interpret_customer_preferences({"dislikes": [dislike]})["sensitivityLevel"] == "high"


@pytest.mark.parametrize("dislikes", [["oud", "strong scents"], ["strong perfume"], ["strong fragrance"]])
def test_interpret_customer_preferences_strong_scent_phrasing(dislikes):
    assert interpret_customer_preferences({"dislikes": dislikes})["sensitivityLevel"] == "high"


def test_interpret_customer_preferences_strong_preference_is_not_sensitivity():
    assert interpret_customer_preferences({"dislikes": ["I have a strong preference for citrus"]})["sensitivityLevel"] == "none"


def test_interpret_customer_preferences_unrelated_dislike():
    assert interpret_customer_preferences({"dislikes": ["I don't like vanilla"]})["sensitivityLevel"] == "none"


def test_compute_complexity_level_thresholds():
    assert compute_complexity_level(3) == "low"
    assert compute_complexity_level(6) == "low"
    assert compute_complexity_level(7) == "moderate"
    assert compute_complexity_level(12) == "moderate"
    assert compute_complexity_level(13) == "high"
    assert compute_complexity_level(20) == "high"
    assert compute_complexity_level(21) == "very-high"
    assert compute_complexity_level(27) == "very-high"


def test_passes_intensity_filter_excludes_three_plus_drivers():
    intent = {"sensitivityLevel": "high"}
    assert passes_intensity_filter(["Black Pepper", "Saffron", "Cinnamon", "Bergamot"], intent) is False


def test_passes_intensity_filter_allows_single_strong_note():
    intent = {"sensitivityLevel": "high"}
    assert passes_intensity_filter(["Black Pepper", "Bergamot", "Lavender"], intent) is True


def test_passes_intensity_filter_excludes_very_high_complexity():
    intent = {"sensitivityLevel": "high"}
    many_mild_notes = [f"Mild Note {i}" for i in range(22)]
    assert passes_intensity_filter(many_mild_notes, intent) is False


def test_passes_intensity_filter_never_filters_without_sensitivity():
    assert passes_intensity_filter(["Black Pepper", "Saffron", "Cinnamon", "Oud"], {"sensitivityLevel": "none"}) is True
    assert passes_intensity_filter(["Black Pepper", "Saffron", "Cinnamon", "Oud"], None) is True


def test_powdery_family_and_almond_context():
    assert "powdery" in detect_families(["Orris"], PREFERENCE_FAMILIES)
    assert "powdery" in detect_families(["Iris", "Violet"], PREFERENCE_FAMILIES)
    assert "powdery" in detect_families(["Heliotrope"], PREFERENCE_FAMILIES)
    assert text_to_preference_families(["Powdery"]) == ["powdery"]
    assert classify_almond_character(["Almond", "Bergamot"]) == "nutty"
    assert classify_almond_character(["Almond", "Iris"]) == "powdery"
    assert classify_almond_character(["Almond", "Orris"]) == "powdery"
    assert classify_almond_character(["Almond", "Vanilla"]) == "gourmand"
    assert classify_almond_character(["Almond", "Tonka Bean"]) == "gourmand"
    assert classify_almond_character(["Bergamot", "Musk"]) is None
    assert "powdery" not in detect_families(["Almond"], PREFERENCE_FAMILIES)


def test_floral_family_present_and_graded():
    assert "floral" in PREFERENCE_FAMILIES
    assert text_to_preference_families(["Floral"]) == ["floral"]
    assert literal_note_terms_from_likes(["Floral"]) == []
    assert "floral" in detect_families(["White Flowers"], PREFERENCE_FAMILIES)
    assert "floral" in detect_families(["Floral Undertones (Violet)"], PREFERENCE_FAMILIES)

    for note in ["Jasmine", "Rose", "Hedione", "Tuberose", "Peony", "Orange Blossom", "Magnolia", "Freesia", "Gardenia", "Ylang-Ylang", "Osmanthus", "Cyclamen", "Lotus", "Mimosa"]:
        assert "floral" in detect_families([note], PREFERENCE_FAMILIES)

    assert "floral" in detect_families(["Lily of the Valley"], PREFERENCE_FAMILIES)
    assert "floral" in detect_families(["Lily-of-the-Valley"], PREFERENCE_FAMILIES)
    assert "floral" in detect_families(["Lily"], PREFERENCE_FAMILIES)

    for note in ["Orchid", "Carnation", "Lilac", "Narcissus"]:
        assert "floral" in detect_families([note], PREFERENCE_FAMILIES)


def test_floral_overlap_decisions():
    for note in ["Iris", "Violet", "Orris"]:
        families = detect_families([note], PREFERENCE_FAMILIES)
        assert "floral" in families
        assert "powdery" in families

    for note in ["Geranium", "Hedione"]:
        families = detect_families([note], PREFERENCE_FAMILIES)
        assert "floral" in families
        assert "powdery" not in families
        assert "fresh" not in families

    assert detect_families(["Neroli"], PREFERENCE_FAMILIES) == ["fresh"]
    assert "floral" not in detect_families(["Lavender"], PREFERENCE_FAMILIES)
    assert "aromatic" in detect_families(["Lavender"], COMPATIBILITY_TAGS)


def test_floral_graded_matching():
    weak = ["Lemon", "Green Tea", "Ginger", "Peach", "Hedione", "Jasmine", "Apple", "Marshmallow", "Vanilla", "Benzoin"]
    strong = ["Rose", "Jasmine", "Tuberose", "Orange Blossom", "Peony", "Lily of the Valley"]
    assert like_match_strength(weak, "floral") == pytest.approx(2 / 10)
    assert like_match_strength(strong, "floral") == 1
    assert like_match_strength(strong, "floral") > like_match_strength(weak, "floral")

    notes = ["Bergamot", "Lemon", "Spearmint", "Peppermint", "Apple", "Pineapple", "Vanilla"]
    assert like_match_strength(notes, "floral") == 0
    assert matched_likes(notes, ["floral"]) == []
    assert matched_likes(["Rose", "Jasmine", "Musk"], ["floral"]) == ["floral"]


def test_floral_known_rosemary_limitation():
    assert "floral" in detect_families(["Rosemary Oil", "Cedar Leaf"], PREFERENCE_FAMILIES)


def test_family_breadth_coverage_score():
    assert family_breadth_coverage_score(0) == 0
    assert family_breadth_coverage_score(1) == 0
    assert family_breadth_coverage_score(2) == 6
    assert family_breadth_coverage_score(3) == 9
    assert family_breadth_coverage_score(4) == 12


def test_assess_combination_risks_powdery_overload():
    products = [{"title": "A", "notes": ["Orris", "Bergamot"]}, {"title": "B", "notes": ["Iris", "Musk"]}]
    assert any("powdery" in r for r in assess_combination_risks(products))

    products = [{"title": "A", "notes": ["Orris", "Bergamot"]}, {"title": "B", "notes": ["Vanilla", "Musk"]}]
    assert not any("powdery" in r for r in assess_combination_risks(products))


def test_interpret_lifestyle_context_gym():
    result = interpret_lifestyle_context({"occasion": "for the gym, working out most mornings"})
    assert "gym" in result["lifestyles"]
    assert {"airy", "crisp"} <= set(result["preferredDirections"].keys())


def test_interpret_lifestyle_context_relaxation():
    result = interpret_lifestyle_context({"occasion": "just want to relax and unwind at home"})
    assert "relaxation" in result["lifestyles"]
    assert "relaxing" in result["preferredDirections"]


def test_interpret_lifestyle_context_safety_officer():
    result = interpret_lifestyle_context({"occasion": "I work as a safety officer"})
    assert "office" in result["lifestyles"]


def test_interpret_lifestyle_context_multiple_lifestyles_weighted():
    result = interpret_lifestyle_context({"occasion": "I work in an office, hit the gym after work, then like to unwind in the evening"})
    assert {"office", "gym", "relaxation"} <= set(result["lifestyles"])
    for weight in result["preferredDirections"].values():
        assert 0 < weight <= 1


def test_interpret_lifestyle_context_empty_when_no_match():
    result = interpret_lifestyle_context({"occasion": "just want something nice"})
    assert result["lifestyles"] == []
    assert len(result["preferredDirections"]) == 0


def test_direction_match_counters():
    assert count_preferred_direction_matches(["Lavender", "Musk", "Chamomile"], ["relaxing"]) > 0
    assert count_avoided_direction_matches(["Black Pepper", "Saffron"], ["pepper-heavy", "dense-spicy"]) > 0
    assert count_avoided_direction_matches(["Black Pepper"], ["oud", "leather"]) == 0


def test_dry_earthy_natural_sensory_directions():
    assert "dry" in text_to_preference_families(["dry"])
    assert "earthy" in text_to_preference_families(["earthy"])
    assert "natural" in text_to_preference_families(["natural"])

    assert "dry" in detect_families(["Vetiver", "Oakmoss"], PREFERENCE_FAMILIES)
    assert "earthy" in detect_families(["Patchouli", "Galbanum"], PREFERENCE_FAMILIES)
    assert "natural" in detect_families(["Sage", "Sea Salt"], PREFERENCE_FAMILIES)

    families = text_to_preference_families(["dry, earthy, natural"])
    assert {"dry", "earthy", "natural"} <= set(families)
    combined = detect_families(["Vetiver", "Moss", "Sage"], PREFERENCE_FAMILIES)
    assert {"dry", "earthy", "natural"} <= set(combined)

    assert literal_note_terms_from_likes(["dry, earthy, natural"]) == []


def test_split_dislikes_by_exactness():
    result = split_dislikes_by_exactness(["Sandalwood"])
    assert "sandalwood" in result["exactNoteDislikes"]
    assert "woody" not in result["explicitFamilyDislikes"]

    result = split_dislikes_by_exactness(["Woody fragrances"])
    assert "woody" in result["explicitFamilyDislikes"]
    assert result["exactNoteDislikes"] == []

    result = split_dislikes_by_exactness(["Amber"])
    assert "amber" in result["exactNoteDislikes"]
    assert "strongHeavy" not in result["explicitFamilyDislikes"]

    result = split_dislikes_by_exactness(["Sandalwood", "Fruity fragrances"])
    assert "sandalwood" in result["exactNoteDislikes"]
    assert "fruity" in result["explicitFamilyDislikes"]


def test_detect_intensity_drivers_used_by_passes_intensity_filter():
    assert len(detect_intensity_drivers(["Black Pepper", "Saffron", "Cinnamon"])) >= 3
