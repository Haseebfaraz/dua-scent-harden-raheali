"""Port of app/utils/recommendationSelectionParser.js -- deterministic resolution of "which
recommendation did the customer mean," so the backend never asks the model to reconstruct a
choice from product names/descriptions. Resolves only against the currently active
recommendation list for this conversation, in rank order.
"""

import re

_WRITTEN_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "last": -1}
_AFFIRMATION_WORDS = {"good", "goof", "fine", "great", "perfect", "yes", "ok", "okay", "this"}
_WORD_SPLIT = re.compile(r"[\s,.'\"!?-]+")
_DIGIT = re.compile(r"\b(\d+)\b")


def _levenshtein1(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1:
        return False
    if a == b:
        return True
    i = j = mismatches = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        mismatches += 1
        if mismatches > 1:
            return False
        if len(a) == len(b):
            i += 1
            j += 1
        elif len(a) > len(b):
            i += 1
        else:
            j += 1
    return True


def _fuzzy_includes(words: list[str], target: str) -> bool:
    return any(_levenshtein1(w, target) for w in words)


def parse_recommendation_selection(text: str | None, active_list: list[dict]) -> dict:
    if not active_list:
        return {"noMatch": True}
    normalized = (text or "").lower().strip()
    words = [w for w in _WORD_SPLIT.split(normalized) if w]

    digit_match = _DIGIT.search(normalized)
    rank = int(digit_match.group(1)) if digit_match else None

    if rank is None:
        for w in words:
            if w in _WRITTEN_NUMBERS:
                rank = _WRITTEN_NUMBERS[w]
                break
            if w in _ORDINALS:
                rank = len(active_list) if _ORDINALS[w] == -1 else _ORDINALS[w]
                break

    if rank is None:
        has_affirmation = any(w in _AFFIRMATION_WORDS for w in words) or _fuzzy_includes(words, "good")
        if has_affirmation and len(active_list) == 1:
            return {"recommendationId": active_list[0]["recommendationId"]}
        return {"noMatch": True}

    if rank < 1 or rank > len(active_list):
        return {"noMatch": True}
    return {"recommendationId": active_list[rank - 1]["recommendationId"]}
