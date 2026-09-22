import pytest
from sqlalchemy import select

from app.db.models import ExistingCombination, FragranceProduct
from app.fragrance.compatibility import (
    exact_note_coverage_score,
    family_breadth_coverage_score,
    literal_note_match_count,
    literal_note_terms_from_likes,
    matched_literal_terms,
    missing_literal_terms,
    split_dislikes_by_exactness,
    text_to_preference_families,
)
from app.fragrance.scoring import classify_dislike_conflict, like_match_strength
from app.services import copy_generation
from app.services.order_history import analyze_customer_product_candidates
from app.services.recommendation_engine import (
    MAX_HISTORY_SCORE,
    assign_roles,
    build_fallback_anchors_for_missing_terms,
    compute_evidence_scope,
    compute_history_score,
    compute_ratios,
    generate_new_product_combinations,
    has_hard_excluded_family,
    has_hard_excluded_term,
    validate_combination_shape,
)


pytestmark = pytest.mark.usefixtures("synthetic_catalog")

@pytest.fixture(autouse=True)
def _no_real_openai_calls(monkeypatch):
    # None of these tests assert on generated copy text, and the engine already sets a
    # deterministic customerFacingDescription/WhySuits before copy generation ever runs (its own
    # fallback) -- short-circuiting the real OpenAI call keeps this suite fast/deterministic/free,
    # exactly like the "hard-failure-no-retry" path already covered in test_copy_generation.py.
    async def _no_op(*args, **kwargs):
        return None

    monkeypatch.setattr(copy_generation, "call_copy_model", _no_op)


def test_assign_roles_real_detected_family():
    roled = assign_roles([
        {"title": "A", "notes": ["Bergamot", "Lemon"]},
        {"title": "B", "notes": ["Vanilla", "Sugar"]},
    ])
    assert roled[0]["role"] == "Freshness"
    assert roled[1]["role"] == "Sweetness"


def test_assign_roles_fallback_to_contrast():
    roled = assign_roles([{"title": "A", "notes": ["Zzzznotarealnote"]}])
    assert roled[0]["role"] == "Contrast"
    assert roled[0]["hasDetectedFamily"] is False


def test_assign_roles_floral_counts():
    roled = assign_roles([
        {"title": "Weak", "notes": ["Lemon", "Green Tea", "Ginger", "Peach", "Hedione", "Jasmine", "Apple", "Marshmallow", "Vanilla", "Benzoin"]},
        {"title": "Strong", "notes": ["Rose", "Jasmine", "Tuberose", "Orange Blossom", "Peony", "Lily of the Valley"]},
        {"title": "None", "notes": ["Bergamot", "Lemon", "Spearmint"]},
    ])
    assert roled[0]["floralNoteCount"] == 2
    assert roled[0]["totalNoteCount"] == 10
    assert roled[0]["floralCoverage"] == pytest.approx(0.2)
    assert roled[1]["floralNoteCount"] == 6
    assert roled[1]["floralCoverage"] == 1
    assert roled[2]["floralNoteCount"] == 0
    assert roled[2]["floralCoverage"] == 0


def test_assign_roles_dominant_family_not_first_match():
    roled = assign_roles([
        {
            "title": "Burlington Gardens",
            "notes": [
                "Grapefruit", "Lime", "Mandarin", "Bitter Orange", "Mint",
                "Ginger", "Cinnamon", "Cumin", "Saffron",
                "Patchouli", "Oakmoss", "Cedar Wood", "Cashmere Wood",
                "Rum", "Tobacco", "Benzoin", "Vanilla", "Labdanum", "Ambergris", "Musk",
            ],
        },
        {"title": "White Hot Chocolate & Rum", "notes": ["Peppermint", "White Chocolate", "Marshmallows", "Whipped Cream", "Rum"]},
    ])
    assert roled[0]["role"] == "Musk/wood base"
    assert roled[1]["role"] == "Sweetness"


def test_like_match_strength_low_fraction_for_buried_token():
    lady_elixir_notes = [
        "Bergamot", "Rose de Mai", "Jasmine from Grasse", "Lily of the Valley", "Heliotrope",
        "Ylang-Ylang", "Geranium", "Violet Leaves", "Peach", "Raspberry", "Cinnamon", "Kashmir",
        "Cedar", "Vanilla", "Iris", "Musk", "Sandalwood", "Musk Mallow",
    ]
    strength = like_match_strength(lady_elixir_notes, "sweet")
    assert strength == pytest.approx(1 / 18, abs=1e-5)
    assert strength < 0.1


def test_like_match_strength_high_fraction_when_dominant():
    assert like_match_strength(["Vanilla", "Sugar", "Caramel", "Honey", "Cedar"], "sweet") == 0.8


def test_like_match_strength_zero():
    assert like_match_strength(["Oud", "Leather", "Smoke"], "sweet") == 0


def test_compute_history_score_caps_full_stacking():
    anchor = {"sameCityOrders": 50, "sameCountryOrders": 500, "sameStateOrders": 20, "repeatPurchaseCustomers": 3, "distinctSimilarCustomers": 10}
    assert compute_history_score(anchor) == MAX_HISTORY_SCORE
    assert MAX_HISTORY_SCORE < 17


def test_compute_history_score_single_axis_below_cap():
    anchor = {"sameCityOrders": 10, "sameCountryOrders": 0, "sameStateOrders": 0, "repeatPurchaseCustomers": 0, "distinctSimilarCustomers": 0}
    assert compute_history_score(anchor) == 5


def test_compute_history_score_zero():
    anchor = {"sameCityOrders": 0, "sameCountryOrders": 0, "sameStateOrders": 0, "repeatPurchaseCustomers": 0, "distinctSimilarCustomers": 0}
    assert compute_history_score(anchor) == 0


def test_compute_history_score_caps_two_stacked_axes():
    anchor = {"sameCityOrders": 1, "sameCountryOrders": 1, "sameStateOrders": 0, "repeatPurchaseCustomers": 0, "distinctSimilarCustomers": 0}
    assert compute_history_score(anchor) == MAX_HISTORY_SCORE


def test_has_hard_excluded_family_trace_note():
    notes = ["Salt", "Watermelon Syrup", "Blueberry Juice", "Mandarin Orange Rinds", "Sandalwood", "White Musk"]
    assert has_hard_excluded_family(notes, ["woody"]) is True


def test_has_hard_excluded_family_no_trace():
    notes = ["Peach Purée", "Strawberry Purée", "Vodka", "White Musk"]
    assert has_hard_excluded_family(notes, ["woody"]) is False


def test_has_hard_excluded_family_noop_when_empty():
    assert has_hard_excluded_family(["Sandalwood"], []) is False
    assert has_hard_excluded_family(["Sandalwood"], None) is False


def test_has_hard_excluded_term():
    assert has_hard_excluded_term(["Gin", "Mojito", "Jackfruit"], ["jackfruit"]) is True
    assert has_hard_excluded_term(["Gin", "Mojito", "Coconut"], ["jackfruit"]) is False
    assert has_hard_excluded_term(["Jackfruit"], []) is False
    assert has_hard_excluded_term(["Jackfruit"], None) is False


def test_compute_ratios_always_sums_to_100_and_34ml():
    cases = [
        [{"title": "A", "role": "Freshness"}, {"title": "B", "role": "Sweetness"}],
        [{"title": "A", "role": "Main fruit body"}, {"title": "B", "role": "Main fruit body"}],
        [{"title": "A", "role": "Freshness"}, {"title": "B", "role": "Main fruit body"}, {"title": "C", "role": "Sweetness"}],
        [
            {"title": "A", "role": "Freshness"}, {"title": "B", "role": "Main fruit body"},
            {"title": "C", "role": "Sweetness"}, {"title": "D", "role": "Longevity support"},
        ],
    ]
    for roled_products in cases:
        ratios = compute_ratios(roled_products)
        pct_sum = sum(r["ratioPercent"] for r in ratios)
        ml_sum = round(sum(r["milliliters"] for r in ratios) * 10) / 10
        assert pct_sum == 100
        assert ml_sum == 34


def test_compute_ratios_two_balanced_fruity_is_1_to_1():
    ratios = compute_ratios([{"title": "A", "role": "Main fruit body"}, {"title": "B", "role": "Main fruit body"}])
    assert ratios[0]["ratioPercent"] == 50
    assert ratios[1]["ratioPercent"] == 50


def test_compute_ratios_caps_sweet_heavy_component():
    ratios = compute_ratios([{"title": "A", "role": "Sweetness"}, {"title": "B", "role": "Contrast"}])
    sweetness = next(r for r in ratios if r["productTitle"] == "A")
    assert sweetness["ratioPercent"] <= 50


def test_validate_combination_shape_accepts_valid_hybrid():
    validate_combination_shape("HYBRID", [{"title": "A"}, {"title": "B"}], [{"ratioPercent": 50}, {"ratioPercent": 50}])


def test_validate_combination_shape_rejects_single_product():
    with pytest.raises(ValueError, match="exactly 2 distinct"):
        validate_combination_shape("HYBRID", [{"title": "A"}], [{"ratioPercent": 100}])


def test_validate_combination_shape_rejects_duplicate_product():
    with pytest.raises(ValueError, match="exactly 2 distinct"):
        validate_combination_shape("HYBRID", [{"title": "A"}, {"title": "A"}])


def test_validate_combination_shape_rejects_wrong_tribrid_count():
    with pytest.raises(ValueError, match="exactly 3 distinct"):
        validate_combination_shape("TRIBRID", [{"title": "A"}, {"title": "B"}])


def test_validate_combination_shape_rejects_wrong_quadbrid_count():
    with pytest.raises(ValueError, match="exactly 4 distinct"):
        validate_combination_shape("QUADBRID", [{"title": "A"}, {"title": "B"}, {"title": "C"}])


def test_validate_combination_shape_rejects_100_percent_one_product():
    with pytest.raises(ValueError, match="100%"):
        validate_combination_shape("HYBRID", [{"title": "A"}, {"title": "B"}], [{"ratioPercent": 100}, {"ratioPercent": 0}])


def test_validate_combination_shape_rejects_ratios_not_summing_to_100():
    with pytest.raises(ValueError, match="sum to 100"):
        validate_combination_shape("HYBRID", [{"title": "A"}, {"title": "B"}], [{"ratioPercent": 40}, {"ratioPercent": 40}])


def test_compute_evidence_scope_never_claims_unverified_location():
    zero = {"sameCityOrders": 0, "sameStateOrders": 0, "sameCountryOrders": 0, "sameSeasonOrders": 0, "distinctSimilarCustomers": 0, "repeatPurchaseCustomers": 0}
    anchor = {**zero, "sameCityOrders": 50, "sameCountryOrders": 500}
    assert compute_evidence_scope(anchor, {"locationVerified": False}) != "city"
    assert compute_evidence_scope(anchor, {"locationVerified": False}) != "country"


def test_compute_evidence_scope_city():
    zero = {"sameCityOrders": 0, "sameStateOrders": 0, "sameCountryOrders": 0, "sameSeasonOrders": 0, "distinctSimilarCustomers": 0, "repeatPurchaseCustomers": 0}
    anchor = {**zero, "sameCityOrders": 12}
    assert compute_evidence_scope(anchor, {"locationVerified": True}) == "city"


def test_compute_evidence_scope_season_global_fallback():
    zero = {"sameCityOrders": 0, "sameStateOrders": 0, "sameCountryOrders": 0, "sameSeasonOrders": 0, "distinctSimilarCustomers": 0, "repeatPurchaseCustomers": 0}
    anchor = {**zero, "sameSeasonOrders": 40}
    assert compute_evidence_scope(anchor, {"locationVerified": True}) == "season_global"


def test_compute_evidence_scope_limited_fallback():
    zero = {"sameCityOrders": 0, "sameStateOrders": 0, "sameCountryOrders": 0, "sameSeasonOrders": 0, "distinctSimilarCustomers": 0, "repeatPurchaseCustomers": 0}
    assert compute_evidence_scope(zero, {"locationVerified": True}) == "limited"


def test_exact_vs_family_dislike_sandalwood():
    split = split_dislikes_by_exactness(["Sandalwood"])
    cedar_vetiver_product = ["Cedar", "Vetiver", "Patchouli", "Musk"]
    assert has_hard_excluded_term(cedar_vetiver_product, split["exactNoteDislikes"]) is False
    assert classify_dislike_conflict(cedar_vetiver_product, split["explicitFamilyDislikes"])["severity"] == "none"


def test_exact_dislike_excludes_containing_product():
    split = split_dislikes_by_exactness(["Sandalwood"])
    assert has_hard_excluded_term(["Bergamot", "Sandalwood", "Musk"], split["exactNoteDislikes"]) is True


def test_broad_family_dislike_still_rejects_via_severity():
    split = split_dislikes_by_exactness(["Woody fragrances"])
    heavily_woody_product = ["Cedar", "Vetiver", "Patchouli", "Guaiac"]
    assert classify_dislike_conflict(heavily_woody_product, split["explicitFamilyDislikes"])["severity"] == "high"


def test_amber_material_policy():
    split = split_dislikes_by_exactness(["Amber"])
    oud_leather_product = ["Oud", "Leather", "Tobacco", "Smoke", "Resin"]
    assert has_hard_excluded_term(oud_leather_product, split["exactNoteDislikes"]) is False
    assert classify_dislike_conflict(oud_leather_product, split["explicitFamilyDislikes"])["severity"] == "none"
    assert has_hard_excluded_term(["Bergamot", "Amber", "Musk"], split["exactNoteDislikes"]) is True


@pytest.mark.asyncio
async def test_bruce_profile_dry_earthy_natural_reaches_candidates(db_session):
    profile = {
        "city": "Liverpool", "stateRegion": None, "country": "United Kingdom", "season": "Winter",
        "likes": ["dry", "earthy", "natural"], "dislikes": ["fruity", "fruity gourmand"], "locationVerified": True,
    }
    candidates = await analyze_customer_product_candidates(db_session, profile)
    assert len(candidates) > 0
    assert any(any(f in ("dry", "earthy", "natural") for f in c["preferenceMatches"]) for c in candidates)


def test_bruce_profile_scoring_mechanism_directly():
    like_families = text_to_preference_families(["dry", "earthy", "natural"])
    dry_earthy_product = ["Vetiver", "Moss", "Galbanum", "Green Tea"]
    sweet_fruit_product = ["Sugar", "Caramel", "Mango", "Pineapple"]
    assert like_match_strength(dry_earthy_product, "dry") > 0
    assert like_match_strength(dry_earthy_product, "earthy") > 0
    assert like_match_strength(dry_earthy_product, "natural") > 0
    dry_earthy_matches = [f for f in like_families if like_match_strength(dry_earthy_product, f) > 0]
    sweet_fruit_matches = [f for f in like_families if like_match_strength(sweet_fruit_product, f) > 0]
    assert len(dry_earthy_matches) > len(sweet_fruit_matches)


# ---- generateNewProductCombinations (real data) ----

_LA_PROFILE = {
    "city": "Los Angeles", "stateRegion": "California", "country": "United States", "season": "Summer",
    "likes": ["Fruity", "Sweet"], "dislikes": ["Spicy", "Strong"],
}


@pytest.mark.asyncio
async def test_never_returns_existing_combination(db_session):
    candidates = await analyze_customer_product_candidates(db_session, _LA_PROFILE)
    combinations = await generate_new_product_combinations(db_session, profile=_LA_PROFILE, candidate_products=candidates)
    for combo in combinations:
        assert combo["existsAlready"] is False
        existing = await db_session.scalar(select(ExistingCombination).where(ExistingCombination.componentKey == combo["canonicalKey"]))
        assert existing is None


@pytest.mark.asyncio
async def test_empty_array_with_no_candidates(db_session):
    combinations = await generate_new_product_combinations(db_session, profile={}, candidate_products=[])
    assert combinations == []


@pytest.mark.asyncio
async def test_ratios_and_component_counts_match_type(db_session):
    profile = {**_LA_PROFILE, "dislikes": []}
    candidates = await analyze_customer_product_candidates(db_session, profile)
    combinations = await generate_new_product_combinations(db_session, profile=profile, candidate_products=candidates, maximum_results=5)
    expected_count = {"HYBRID": 2, "TRIBRID": 3, "QUADBRID": 4}
    for combo in combinations:
        assert len(combo["internalProducts"]) == expected_count[combo["type"]]
        assert sum(r["ratioPercent"] for r in combo["recommendedRatio"]) == 100


_ORDINARY_PROFILE = {**_LA_PROFILE, "dislikes": [], "locationVerified": True}


@pytest.mark.asyncio
async def test_type_simplicity_bias_hybrid_default(db_session):
    candidates = await analyze_customer_product_candidates(db_session, _ORDINARY_PROFILE)
    hybrid_results = await generate_new_product_combinations(db_session, profile=_ORDINARY_PROFILE, candidate_products=candidates, allowed_types=["HYBRID"], maximum_results=5)
    quadbrid_results = await generate_new_product_combinations(db_session, profile=_ORDINARY_PROFILE, candidate_products=candidates, allowed_types=["QUADBRID"], maximum_results=5)
    assert len(hybrid_results) > 0
    assert len(quadbrid_results) > 0
    for r in hybrid_results:
        assert r["typeSimplicityScore"] == 10
    for r in quadbrid_results:
        assert r["typeSimplicityScore"] == -16


@pytest.mark.asyncio
async def test_type_simplicity_bias_strengthens_for_sensitive_customer(db_session):
    sensitive_profile = {**_ORDINARY_PROFILE, "dislikes": ["too strong"]}
    candidates = await analyze_customer_product_candidates(db_session, sensitive_profile)
    hybrid_results = await generate_new_product_combinations(db_session, profile=sensitive_profile, candidate_products=candidates, allowed_types=["HYBRID"], maximum_results=5)
    for r in hybrid_results:
        assert r["typeSimplicityScore"] == 14


@pytest.mark.asyncio
async def test_never_exclusively_quadbrid_for_ordinary_customer(db_session):
    candidates = await analyze_customer_product_candidates(db_session, _ORDINARY_PROFILE)
    combinations = await generate_new_product_combinations(db_session, profile=_ORDINARY_PROFILE, candidate_products=candidates, maximum_results=8)
    types = {c["type"] for c in combinations}
    if types:
        assert not ("QUADBRID" in types and len(types) == 1)


@pytest.mark.asyncio
async def test_exposes_real_product_names_and_notes(db_session):
    candidates = await analyze_customer_product_candidates(db_session, _ORDINARY_PROFILE)
    combinations = await generate_new_product_combinations(db_session, profile=_ORDINARY_PROFILE, candidate_products=candidates, maximum_results=5)
    assert len(combinations) > 0
    for combo in combinations:
        real_titles = [p["title"] for p in combo["internalProducts"]]
        assert len(combo["components"]) == len(real_titles)
        for title in real_titles:
            assert any(c["productName"] == title for c in combo["components"])
            assert any(n["label"] == title for n in combo["customerFacingNotesByProduct"])
        for c in combo["components"]:
            assert len(c["availableNotes"]) > 0
            assert isinstance(c["ratioPercent"], (int, float))
            assert c["contribution"]
        assert combo["customerFacingName"]
        assert combo["customerFacingDescription"]


_KARACHI_PROFILE = {
    "city": "Karachi", "country": "Pakistan", "season": "Summer",
    "likes": ["Fruity", "Fresh", "Apple", "Strawberry", "Peach"], "dislikes": ["Amber", "Sandalwood"],
    "occasion": "office", "locationVerified": True,
}


@pytest.mark.reference_data  # asserts what the REAL catalog contains; see docs/PLATFORM_MODERNIZATION.md
@pytest.mark.asyncio
async def test_only_returns_combinations_with_literal_named_notes(db_session):
    candidates = await analyze_customer_product_candidates(db_session, _KARACHI_PROFILE)
    combinations = await generate_new_product_combinations(db_session, profile=_KARACHI_PROFILE, candidate_products=candidates, maximum_results=8)
    literal_terms = literal_note_terms_from_likes(_KARACHI_PROFILE["likes"])
    assert len(combinations) > 0
    for combo in combinations:
        all_notes = [n for p in combo["internalProducts"] for n in (p.get("notes") or [])]
        assert literal_note_match_count(all_notes, literal_terms) > 0


@pytest.mark.reference_data  # asserts what the REAL catalog contains; see docs/PLATFORM_MODERNIZATION.md
@pytest.mark.asyncio
async def test_covers_every_literal_note_across_final_batch(db_session):
    candidates = await analyze_customer_product_candidates(db_session, _KARACHI_PROFILE)
    combinations = await generate_new_product_combinations(db_session, profile=_KARACHI_PROFILE, candidate_products=candidates, maximum_results=8)
    assert len(combinations) > 0

    literal_terms = literal_note_terms_from_likes(_KARACHI_PROFILE["likes"])
    covered_across_batch = {t for c in combinations for t in c["matchedExactNotes"]}
    for term in literal_terms:
        assert term in covered_across_batch

    for combo in combinations:
        assert isinstance(combo["matchedExactNotes"], list)
        assert isinstance(combo["missingExactNotes"], list)
        assert set(combo["matchedExactNotes"]) | set(combo["missingExactNotes"]) == set(literal_terms)
        assert not any(t in combo["missingExactNotes"] for t in combo["matchedExactNotes"])


@pytest.mark.reference_data  # asserts what the REAL catalog contains; see docs/PLATFORM_MODERNIZATION.md
@pytest.mark.asyncio
async def test_finds_real_fallback_anchors_for_strawberry(db_session):
    products = (await db_session.execute(
        select(FragranceProduct.title, FragranceProduct.normalizedTitle, FragranceProduct.notesJson, FragranceProduct.collection)
    )).all()
    all_products = [{"title": t, "normalizedTitle": nt, "notesJson": nj, "collection": c} for t, nt, nj, c in products]
    anchors = build_fallback_anchors_for_missing_terms(
        ["strawberry"], all_products=all_products, finished_combination_titles=set(), preference_intent={}, candidate_products=[],
    )
    assert len(anchors) > 0
    for anchor in anchors:
        assert len(matched_literal_terms(anchor["orderHistoryNotes"], ["strawberry"])) > 0


@pytest.mark.reference_data  # asserts what the REAL catalog contains; see docs/PLATFORM_MODERNIZATION.md
@pytest.mark.asyncio
async def test_apple_only_pool_still_covers_strawberry_and_peach_via_fallback(db_session):
    products = (await db_session.execute(
        select(FragranceProduct.title, FragranceProduct.normalizedTitle, FragranceProduct.notesJson, FragranceProduct.collection)
    )).all()
    all_products = [{"title": t, "normalizedTitle": nt, "notesJson": nj, "collection": c} for t, nt, nj, c in products]
    apple_only = next(
        (
            p for p in all_products
            if len(matched_literal_terms(p["notesJson"], ["apple"])) > 0
            and len(matched_literal_terms(p["notesJson"], ["strawberry"])) == 0
            and len(matched_literal_terms(p["notesJson"], ["peach"])) == 0
            and str(p["collection"]).lower() not in ("hybrid", "tribrid", "quadbrid")
        ),
        None,
    )
    assert apple_only is not None

    candidate_products = [{
        "productName": apple_only["title"], "normalizedProductName": apple_only["normalizedTitle"], "collection": apple_only["collection"],
        "relevanceScore": 100, "orderHistoryNotes": apple_only["notesJson"],
        "sameCityOrders": 0, "sameStateOrders": 0, "sameCountryOrders": 0, "sameSeasonOrders": 0,
        "distinctSimilarCustomers": 0, "repeatPurchaseCustomers": 0,
    }]
    profile = {"likes": ["Apple", "Strawberry", "Peach"], "dislikes": [], "locationVerified": True}
    combinations = await generate_new_product_combinations(db_session, profile=profile, candidate_products=candidate_products, maximum_results=8)
    assert len(combinations) > 0

    covered_across_batch = {t for c in combinations for t in c["matchedExactNotes"]}
    assert "apple" in covered_across_batch
    assert "strawberry" in covered_across_batch
    assert "peach" in covered_across_batch


@pytest.mark.asyncio
async def test_never_invents_anchor_for_nonexistent_note(db_session):
    products = (await db_session.execute(
        select(FragranceProduct.title, FragranceProduct.normalizedTitle, FragranceProduct.notesJson, FragranceProduct.collection)
    )).all()
    all_products = [{"title": t, "normalizedTitle": nt, "notesJson": nj, "collection": c} for t, nt, nj, c in products]
    anchors = build_fallback_anchors_for_missing_terms(
        ["zzznonexistentnote"], all_products=all_products, finished_combination_titles=set(), preference_intent={}, candidate_products=[],
    )
    assert anchors == []
    assert missing_literal_terms(["Apple", "Musk", "Vanilla"], ["zzznonexistentnote"]) == ["zzznonexistentnote"]


@pytest.mark.reference_data  # asserts what the REAL catalog contains; see docs/PLATFORM_MODERNIZATION.md
@pytest.mark.asyncio
async def test_fallback_batch_still_respects_hard_dislikes_and_no_duplicates(db_session):
    candidates = await analyze_customer_product_candidates(db_session, _KARACHI_PROFILE)
    combinations = await generate_new_product_combinations(db_session, profile=_KARACHI_PROFILE, candidate_products=candidates, maximum_results=8)
    assert len(combinations) > 0

    existing = (await db_session.execute(select(ExistingCombination.componentKey))).all()
    existing_keys = {e[0] for e in existing}

    for combo in combinations:
        for product in combo["internalProducts"]:
            assert literal_note_match_count(product["notes"], ["amber", "sandalwood"]) == 0
        assert combo["canonicalKey"] not in existing_keys


@pytest.mark.reference_data  # asserts what the REAL catalog contains; see docs/PLATFORM_MODERNIZATION.md
@pytest.mark.asyncio
async def test_persists_exact_note_coverage_score(db_session):
    candidates = await analyze_customer_product_candidates(db_session, _KARACHI_PROFILE)
    combinations = await generate_new_product_combinations(db_session, profile=_KARACHI_PROFILE, candidate_products=candidates, maximum_results=8)
    assert len(combinations) > 0
    for combo in combinations:
        assert combo["exactNoteCoverageScore"] == exact_note_coverage_score(len(combo["matchedExactNotes"]))


@pytest.mark.reference_data  # asserts what the REAL catalog contains; see docs/PLATFORM_MODERNIZATION.md
@pytest.mark.asyncio
async def test_preference_score_reflects_exact_note_coverage(db_session):
    candidates = await analyze_customer_product_candidates(db_session, _KARACHI_PROFILE)
    combinations = await generate_new_product_combinations(db_session, profile=_KARACHI_PROFILE, candidate_products=candidates, maximum_results=8)
    literal_terms = literal_note_terms_from_likes(_KARACHI_PROFILE["likes"])
    assert len(combinations) > 0
    for combo in combinations:
        all_notes = [n for p in combo["internalProducts"] for n in (p.get("notes") or [])]
        distinct_matches = literal_note_match_count(all_notes, literal_terms)
        assert distinct_matches > 0
        assert combo["preferenceScore"] >= exact_note_coverage_score(distinct_matches)


@pytest.mark.asyncio
async def test_never_restricts_results_for_style_words_only(db_session):
    candidates = await analyze_customer_product_candidates(db_session, _ORDINARY_PROFILE)
    combinations = await generate_new_product_combinations(db_session, profile=_ORDINARY_PROFILE, candidate_products=candidates, maximum_results=8)
    assert len(combinations) > 0


@pytest.mark.asyncio
async def test_floral_only_customer_every_result_matches_floral(db_session):
    profile = {
        "city": "Las Vegas", "country": "United States", "season": "Summer",
        "likes": ["Floral"], "dislikes": ["Musk", "Oakmoss", "Sandalwood", "Patchouli", "Vetiver"],
        "locationVerified": True,
    }
    candidates = await analyze_customer_product_candidates(db_session, profile)
    results = await generate_new_product_combinations(db_session, profile=profile, candidate_products=candidates, maximum_results=8)
    assert len(results) > 0
    floral_kw = [
        "jasmine", "rose", "violet", "iris", "orris", "tuberose", "magnolia", "freesia", "gardenia",
        "peony", "ylang", "osmanthus", "orange blossom", "lily", "cyclamen", "lotus", "mimosa",
        "geranium", "hedione", "floral", "flower",
    ]
    for r in results:
        assert r["requestedPreferenceFamilies"] == ["floral"]
        assert "floral" in r["matchedPreferenceFamilies"]
        assert r["missingPreferenceFamilies"] == []
        floral_notes = [n for p in r["internalProducts"] for n in (p.get("notes") or []) if any(kw in str(n).lower() for kw in floral_kw)]
        assert len(floral_notes) > 0


@pytest.mark.reference_data  # asserts what the REAL catalog contains; see docs/PLATFORM_MODERNIZATION.md
@pytest.mark.asyncio
async def test_floral_dominant_product_can_win_anchor_role(db_session):
    profile = {"city": "Las Vegas", "country": "United States", "season": "Summer", "likes": ["Floral"], "dislikes": [], "locationVerified": True}
    candidates = await analyze_customer_product_candidates(db_session, profile)
    results = await generate_new_product_combinations(db_session, profile=profile, candidate_products=candidates, maximum_results=8)
    assert len(results) > 0
    assert any(r["floralRoleStrength"] >= 0.3 for r in results)


@pytest.mark.asyncio
async def test_fresh_floral_breadth_bonus(db_session):
    profile = {"city": "Las Vegas", "country": "United States", "season": "Summer", "likes": ["Fresh", "Floral"], "dislikes": [], "locationVerified": True}
    candidates = await analyze_customer_product_candidates(db_session, profile)
    results = await generate_new_product_combinations(db_session, profile=profile, candidate_products=candidates, maximum_results=8)
    assert len(results) > 0
    for r in results:
        assert set(r["requestedPreferenceFamilies"]) >= {"fresh", "floral"}
        assert len(r["missingPreferenceFamilies"]) <= 2
    dual_coverage = [r for r in results if len(r["matchedPreferenceFamilies"]) >= 2]
    single_coverage = [r for r in results if len(r["matchedPreferenceFamilies"]) == 1]
    if dual_coverage and single_coverage:
        assert family_breadth_coverage_score(2) > family_breadth_coverage_score(1)


@pytest.mark.asyncio
async def test_final_batch_coverage_metadata(db_session):
    profile = {
        "city": "Los Angeles", "stateRegion": "California", "country": "United States", "season": "Summer",
        "likes": ["Fruity", "Apple"], "dislikes": [], "locationVerified": True,
    }
    candidates = await analyze_customer_product_candidates(db_session, profile)
    results = await generate_new_product_combinations(db_session, profile=profile, candidate_products=candidates, maximum_results=5)
    for r in results:
        assert "apple" in r["requestedExactNotes"]
        assert isinstance(r["fallbackUsed"], bool)
        assert isinstance(r["fallbackTargetNotes"], list)
