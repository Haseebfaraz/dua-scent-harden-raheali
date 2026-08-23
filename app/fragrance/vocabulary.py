"""Port of app/utils/fragranceVocabulary.js -- a controlled language-variation layer so
customer-facing copy doesn't lean on the same handful of words every time. Picking is
deterministic (no randomness) so the same recommendation always renders the same way.
"""

DIRECTION_VOCABULARY = {
    "light_energetic": [
        "crisp", "airy", "bright", "sparkling", "lively", "invigorating",
        "breezy", "clean-cut", "refreshing", "effortless",
    ],
    "smooth_professional": [
        "polished", "refined", "composed", "sophisticated", "well-balanced",
        "understated", "professional", "modern", "sleek", "confident",
    ],
    "sweet_comforting": [
        "creamy", "soft", "cozy", "indulgent", "playful", "delicious",
        "velvety", "comforting", "gentle", "smoothly sweet",
    ],
    "deep_evening": [
        "luxurious", "magnetic", "dramatic", "sensual", "mysterious",
        "full-bodied", "statement-making", "captivating", "intense", "opulent",
    ],
}

_DIRECTION_BY_ROLE = {
    "Freshness": "light_energetic",
    "Main fruit body": "light_energetic",
    "Sweetness": "sweet_comforting",
    "Floral bridge": "smooth_professional",
    "Musk/wood base": "deep_evening",
    "Longevity support": "deep_evening",
    "Contrast": "smooth_professional",
}


def direction_for_role(role: str) -> str:
    return _DIRECTION_BY_ROLE.get(role, "smooth_professional")


def _js_hash(seed: str) -> int:
    """Replicates JS `hash = (hash * 31 + charCode) >>> 0` (unsigned 32-bit)."""
    h = 0
    for ch in seed:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h


def pick_words(pool: list[str], count: int, seed: str = "", used_words: set[str] | None = None) -> list[str]:
    if used_words is None:
        used_words = set()
    hash_value = _js_hash(seed)

    available = [w for w in pool if w not in used_words]
    source = available if len(available) >= count else pool
    start_index = hash_value % len(source) if source else 0

    picked = []
    for i in range(len(source)):
        if len(picked) >= count:
            break
        picked.append(source[(start_index + i) % len(source)])
    used_words.update(picked)
    return picked


def describe_character(roles: list[str], seed: str, used_words: set[str] | None = None) -> str:
    if used_words is None:
        used_words = set()
    directions = list(dict.fromkeys(direction_for_role(role) for role in roles))
    phrases = []
    for direction in directions:
        words = pick_words(DIRECTION_VOCABULARY[direction], 2, seed + direction, used_words)
        phrases.append(" and ".join(words))
    return ", with a ".join(phrases)
