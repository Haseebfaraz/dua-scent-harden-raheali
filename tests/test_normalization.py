from app.fragrance.normalization import (
    SEASON_ALIASES,
    correct_preference_vocabulary,
    correct_preference_vocabulary_list,
    normalize_product_name,
    normalize_region_text,
    resolve_location_input,
)


def test_normalize_product_name_lowercases_trims_strips_punctuation():
    assert normalize_product_name("The Opera!") == "the opera"
    assert normalize_product_name("  Water   of   Arabia  ") == "water of arabia"


def test_normalize_product_name_empty_for_falsy_input():
    assert normalize_product_name(None) == ""
    assert normalize_product_name("") == ""


def test_normalize_region_text():
    assert normalize_region_text("St. Clair Shores") == "st clair shores"
    assert normalize_region_text("Cote d'Ivoire") == "cote divoire"


_REGION_MAPS = {
    "city": {"los angeles": "Los Angeles"},
    "stateName": {"california": "California"},
    "countryName": {"united states": "United States"},
}


def test_resolve_location_input_known_typos():
    assert resolve_location_input("Los Angelos", _REGION_MAPS) == {
        "resolved": True, "needsConfirmation": False, "field": "city",
        "value": "Los Angeles", "wasCorrected": True, "originalInput": "Los Angelos",
    }
    result = resolve_location_input("Califronia", _REGION_MAPS)
    assert result["resolved"] is True
    assert result["field"] == "stateName"
    assert result["value"] == "California"
    assert result["wasCorrected"] is True

    result = resolve_location_input("United State", _REGION_MAPS)
    assert result["field"] == "countryName"
    assert result["value"] == "United States"
    assert result["wasCorrected"] is True


def test_resolve_location_input_exact_match_no_correction():
    result = resolve_location_input("Los Angeles", _REGION_MAPS)
    assert result["resolved"] is True
    assert result["wasCorrected"] is False


def test_resolve_location_input_unrecognized_needs_confirmation():
    result = resolve_location_input("Xyzzyville", _REGION_MAPS)
    assert result["resolved"] is False
    assert result["needsConfirmation"] is True
    assert result["value"] is None


def test_resolve_location_input_empty_input():
    for value in ("", None):
        result = resolve_location_input(value, _REGION_MAPS)
        assert result["resolved"] is False
        assert result["needsConfirmation"] is False


def test_correct_preference_vocabulary_acceptance_scenarios():
    assert correct_preference_vocabulary("spricy")["corrected"] == "spicy"
    assert correct_preference_vocabulary("gourmant")["corrected"] == "gourmand"
    assert correct_preference_vocabulary("fruty")["corrected"] == "fruity"
    assert correct_preference_vocabulary("aquitic")["corrected"] == "aquatic"


def test_correct_preference_vocabulary_within_sentence():
    result = correct_preference_vocabulary("I like spricy and fruty scents for the gym")
    assert result["corrected"] == "I like spicy and fruity scents for the gym"
    assert {"original": "spricy", "corrected": "spicy"} in result["corrections"]
    assert {"original": "fruty", "corrected": "fruity"} in result["corrections"]


def test_correct_preference_vocabulary_preserves_capitalization():
    assert correct_preference_vocabulary("Spricy")["corrected"] == "Spicy"
    assert correct_preference_vocabulary("SPRICY")["corrected"] == "SPICY"


def test_correct_preference_vocabulary_leaves_unlisted_words_untouched():
    assert correct_preference_vocabulary("I like spicy and woody scents")["corrected"] == "I like spicy and woody scents"
    assert correct_preference_vocabulary("A totally unrelated sentence about cats")["corrected"] == "A totally unrelated sentence about cats"


def test_correct_preference_vocabulary_never_touches_product_names():
    assert normalize_product_name("Fruty") == "fruty"


def test_correct_preference_vocabulary_empty_input():
    assert correct_preference_vocabulary("") == {"corrected": "", "corrections": []}
    assert correct_preference_vocabulary(None) == {"corrected": "", "corrections": []}


def test_correct_preference_vocabulary_list():
    result = correct_preference_vocabulary_list(["Spricy", "Woody", "Gourmant"])
    assert result["corrected"] == ["Spicy", "Woody", "Gourmand"]
    assert {"original": "Spricy", "corrected": "spicy"} in result["corrections"]
    assert {"original": "Gourmant", "corrected": "gourmand"} in result["corrections"]


def test_correct_preference_vocabulary_list_empty():
    assert correct_preference_vocabulary_list([]) == {"corrected": [], "corrections": []}


def test_season_aliases_cover_all_four_seasons():
    assert "Winter Months" in SEASON_ALIASES["Winter"]
    assert "Autumn Months" in SEASON_ALIASES["Fall"]
    assert sorted(SEASON_ALIASES.keys()) == sorted(["Fall", "Spring", "Summer", "Winter"])
