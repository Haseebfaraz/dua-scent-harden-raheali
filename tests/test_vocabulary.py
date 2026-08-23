import re

from app.fragrance.vocabulary import DIRECTION_VOCABULARY, describe_character, direction_for_role, pick_words


def test_direction_for_role_mapping():
    assert direction_for_role("Freshness") == "light_energetic"
    assert direction_for_role("Main fruit body") == "light_energetic"
    assert direction_for_role("Musk/wood base") == "deep_evening"
    assert direction_for_role("Longevity support") == "deep_evening"


def test_direction_for_role_fallback():
    assert direction_for_role("NotARealRole") == "smooth_professional"


def test_pick_words_deterministic():
    a = pick_words(DIRECTION_VOCABULARY["light_energetic"], 2, "combo-abc")
    b = pick_words(DIRECTION_VOCABULARY["light_energetic"], 2, "combo-abc")
    assert a == b


def test_pick_words_never_repeats_used_word_across_batch():
    used_words = set()
    first = pick_words(DIRECTION_VOCABULARY["light_energetic"], 2, "seed-1", used_words)
    second = pick_words(DIRECTION_VOCABULARY["light_energetic"], 2, "seed-2", used_words)
    assert not (set(first) & set(second))


def test_describe_character_only_uses_words_from_the_roles_directions():
    used_words = set()
    description = describe_character(["Freshness", "Sweetness"], "combo-1", used_words)
    all_words = DIRECTION_VOCABULARY["light_energetic"] + DIRECTION_VOCABULARY["sweet_comforting"]
    for word in [w.strip() for w in re.split(r",?\s+with a\s+|\s+and\s+", description) if w.strip()]:
        assert word in all_words
