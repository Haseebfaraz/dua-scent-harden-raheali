from app.fragrance.selection_parser import parse_recommendation_selection

_ACTIVE_LIST = [{"recommendationId": "rec_1"}, {"recommendationId": "rec_2"}, {"recommendationId": "rec_3"}]


def test_resolves_plain_digit():
    assert parse_recommendation_selection("1", _ACTIVE_LIST) == {"recommendationId": "rec_1"}


def test_resolves_option_1():
    assert parse_recommendation_selection("Option 1", _ACTIVE_LIST) == {"recommendationId": "rec_1"}


def test_resolves_opt_1_is_good():
    assert parse_recommendation_selection("opt 1 is good", _ACTIVE_LIST) == {"recommendationId": "rec_1"}


def test_resolves_real_transcript_typo_goof():
    assert parse_recommendation_selection("opt 1 is goof", _ACTIVE_LIST) == {"recommendationId": "rec_1"}


def test_resolves_written_numbers():
    assert parse_recommendation_selection("I'll take option two", _ACTIVE_LIST) == {"recommendationId": "rec_2"}


def test_resolves_ordinals():
    assert parse_recommendation_selection("the first one please", _ACTIVE_LIST) == {"recommendationId": "rec_1"}
    assert parse_recommendation_selection("give me the second", _ACTIVE_LIST) == {"recommendationId": "rec_2"}
    assert parse_recommendation_selection("I want the last one", _ACTIVE_LIST) == {"recommendationId": "rec_3"}


def test_resolves_common_selection_phrases():
    assert parse_recommendation_selection("let's go with number 3", _ACTIVE_LIST) == {"recommendationId": "rec_3"}
    assert parse_recommendation_selection("choose 1", _ACTIVE_LIST) == {"recommendationId": "rec_1"}


def test_no_match_out_of_range():
    assert parse_recommendation_selection("option 9", _ACTIVE_LIST) == {"noMatch": True}


def test_no_match_unrelated_text():
    assert parse_recommendation_selection("what's the weather like", _ACTIVE_LIST) == {"noMatch": True}


def test_resolves_bare_affirmation_when_only_one_active():
    assert parse_recommendation_selection(
        "this one is fine, create it", [{"recommendationId": "only_one"}]
    ) == {"recommendationId": "only_one"}


def test_no_match_when_list_empty():
    assert parse_recommendation_selection("option 1", []) == {"noMatch": True}
