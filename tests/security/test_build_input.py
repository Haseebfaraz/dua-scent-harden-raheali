"""Phase 1 regression tests for N1: one authoritative validator for customer-controlled ratios
and the custom fragrance name (app/shopify/build_input.py)."""


import pytest

from app.shopify.build_input import (
    MAX_LAYER_PERCENT,
    MAX_NAME_LENGTH,
    MIN_LAYER_PERCENT,
    InvalidCustomName,
    InvalidRatios,
    validate_custom_name,
    validate_ratios,
)


def _r(top, middle, base):
    return {"top": top, "middle": middle, "base": base}


# ---------------------------------------------------------------------------
# Ratios: every price-manipulation shape from the audit must be rejected
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ratios", [
    _r(1, 1, 1),                       # total far below 100 (the ~$4 bottle)
    _r(0, 0, 0),
    _r(-100, 100, 100),                # sums to 100 with a negative layer
    _r(100, 100, -100),
    _r(-10, 60, 50),
    _r(150, -25, -25),
    _r(101, 0, -1),
    _r(50, 50, 50),                    # total above 100
    _r(34, 33, 32),                    # total below 100 by one
    _r(34, 33, 34),                    # total above 100 by one
    _r(0, 50, 50),                     # zero layer
    _r(100, 0, 0),                     # zero layers (a bottle is always three layers)
    _r(-1, 51, 50),
    _r(10**9, 10**9, -2 * 10**9 + 100),
    _r(float("nan"), 50, 50),
    _r(float("inf"), 50, 50),
    _r(float("-inf"), 50, 50),
    _r(50, float("nan"), float("nan")),
    _r(33.4, 33.3, 33.3),              # non-integer percentages
    _r(34.5, 33.5, 32.0),
    _r("34", "33", "33"),              # strings masquerading as numbers
    _r("34", 33, 33),
    _r(True, 33, 66),                  # bool is not a percentage
    _r(None, 50, 50),
    _r([34], [33], [33]),
    _r({"v": 34}, 33, 33),
    {"top": 50, "middle": 50},         # missing position
    {"top": 34, "middle": 33},         # missing base
    {"top": 34, "middle": 33, "base": 33, "extra": 0},   # extra position
    {"top": 34, "middle": 33, "base": 33, "Top": 0},
    {"TOP": 34, "MIDDLE": 33, "BASE": 33},
    {},
    [],
    [34, 33, 33],
    "34/33/33",
    None,
    42,
])
def test_invalid_ratios_are_rejected(ratios):
    with pytest.raises(InvalidRatios):
        validate_ratios(ratios)


@pytest.mark.parametrize("ratios, expected", [
    (_r(34, 33, 33), _r(34, 33, 33)),
    (_r(50, 25, 25), _r(50, 25, 25)),
    (_r(40, 30, 30), _r(40, 30, 30)),
    (_r(5, 5, 90), _r(5, 5, 90)),
    (_r(98, 1, 1), _r(98, 1, 1)),
    (_r(40.0, 30.0, 30.0), _r(40, 30, 30)),   # integral floats (JSON from JS) are accepted
])
def test_valid_compositions_are_accepted_and_normalized_to_ints(ratios, expected):
    result = validate_ratios(ratios)
    assert result == expected
    assert all(type(v) is int for v in result.values())
    assert sum(result.values()) == 100


def test_layer_bounds_are_consistent():
    assert MIN_LAYER_PERCENT >= 1
    assert MAX_LAYER_PERCENT == 100 - 2 * MIN_LAYER_PERCENT
    with pytest.raises(InvalidRatios):
        validate_ratios(_r(MAX_LAYER_PERCENT + 1, MIN_LAYER_PERCENT, MIN_LAYER_PERCENT - 1))


def test_error_messages_never_echo_the_input():
    with pytest.raises(InvalidRatios) as info:
        validate_ratios(_r("<script>", 1, 1))
    assert "<script>" not in str(info.value)


# ---------------------------------------------------------------------------
# Custom name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Midnight Edition", "Midnight Edition"),
    ("  Midnight   Edition  ", "Midnight Edition"),
    ("Été à Paris", "Été à Paris"),
    ("Café Noir", "Café Noir"),          # NFC normalized
    ("東京の夜", "東京の夜"),
    ("Rosé & Oud #1", "Rosé & Oud #1"),
    ("a", "a"),
    ("x" * MAX_NAME_LENGTH, "x" * MAX_NAME_LENGTH),
])
def test_valid_names_are_cleaned_not_rejected(raw, expected):
    assert validate_custom_name(raw) == expected


@pytest.mark.parametrize("raw", [
    "x" * (MAX_NAME_LENGTH + 1),
    "x" * 5000,
    "Name\x00WithNull",
    "Name\x1bWithEscape",
    "Name​ZeroWidth",
    "Name‮Reversed",
    "Line Separator",
    "Tab\tInside",   # control character
    "New\nLine",
    123,
    ["Name"],
    {"name": "x"},
])
def test_invalid_names_are_rejected(raw):
    with pytest.raises(InvalidCustomName):
        validate_custom_name(raw)


@pytest.mark.parametrize("raw", [None, "", "   ", "\n\n"])
def test_missing_name_is_none_unless_required(raw):
    assert validate_custom_name(raw) is None
    with pytest.raises(InvalidCustomName):
        validate_custom_name(raw, required=True)


def test_name_error_messages_never_echo_the_input():
    with pytest.raises(InvalidCustomName) as info:
        validate_custom_name("<img src=x>\x00")
    assert "<img" not in str(info.value)
