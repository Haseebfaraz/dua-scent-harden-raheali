"""Port of app/utils/notePositionMapping.js -- deterministic Top/Middle/Base note-position
bucketing. Never invents a note or changes its wording; only decides which of the three display
groups each real note falls into, using its own keyword content, never randomly.
"""

from app.fragrance.compatibility import PREFERENCE_FAMILIES, contains_whole_word

_LITERAL_MATCH_RANK = 2
_FAMILY_MATCH_RANK = 1

# Order matters: each keyword list is checked top-to-bottom, first match wins.
_TOP_KEYWORDS = [
    "citrus", "bergamot", "lemon", "lime", "mandarin", "grapefruit", "orange", "tangerine",
    "fresh", "aquatic", "marine", "sea salt", "galbanum", "mint", "neroli", "petitgrain",
    "aromatic", "sicilian", "calabrian", "amalfi",
]
_MIDDLE_KEYWORDS = [
    "floral", "jasmine", "rose", "violet", "iris", "tuberose", "magnolia", "freesia",
    "gardenia", "peony", "ylang", "osmanthus", "orange blossom", "geranium", "lavender",
    "tea", "green tea", "herbal", "sage", "basil", "thyme", "rosemary",
    "spice", "spicy", "cardamom", "coriander", "pink pepper", "cinnamon",
    "pear", "peach", "apricot", "berr", "strawberry", "raspberry", "fig", "apple", "pineapple", "mango",
]
_BASE_KEYWORDS = [
    "musk", "wood", "sandalwood", "cedar", "vetiver", "patchouli", "guaiac", "oud", "agarwood",
    "vanilla", "benzoin", "amber", "ambergris", "ambroxan", "labdanum", "moss", "oakmoss",
    "resin", "incense", "leather", "tobacco", "tonka", "musky",
]


def classify_note(note: str) -> str:
    lower = str(note).lower()
    if any(kw in lower for kw in _TOP_KEYWORDS):
        return "top"
    if any(kw in lower for kw in _MIDDLE_KEYWORDS):
        return "middle"
    if any(kw in lower for kw in _BASE_KEYWORDS):
        return "base"
    return "middle"


_MIN_NOTES_PER_POSITION = 3
_MAX_NOTES_PER_POSITION = 5


def _note_like_rank(note: str, like_families: list[str], literal_terms: list[str]) -> int:
    if literal_terms and any(contains_whole_word(note, term) for term in literal_terms):
        return _LITERAL_MATCH_RANK
    lower = str(note).lower()
    if like_families:
        for family in like_families:
            keywords = PREFERENCE_FAMILIES.get(family)
            if keywords and any(kw in lower for kw in keywords):
                return _FAMILY_MATCH_RANK
    return 0


def _prioritize_liked_notes(notes: list[str], like_families: list[str], literal_terms: list[str]) -> list[str]:
    if not like_families and not literal_terms:
        return notes
    # Python's sorted() is stable, matching JS Array.prototype.sort (stable since ES2019).
    return sorted(notes, key=lambda n: -_note_like_rank(n, like_families, literal_terms))


def assign_note_positions(
    all_notes: list[str] | None, like_families: list[str] | None = None, literal_terms: list[str] | None = None
) -> dict:
    like_families = like_families or []
    literal_terms = literal_terms or []

    seen = set()
    deduped = []
    for n in all_notes or []:
        key = str(n).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(n)

    buckets: dict[str, list[str]] = {"top": [], "middle": [], "base": []}
    for note in deduped:
        buckets[classify_note(note)].append(note)

    overflow: list[str] = []
    for position in ("top", "middle", "base"):
        if len(buckets[position]) > _MAX_NOTES_PER_POSITION:
            ordered = _prioritize_liked_notes(buckets[position], like_families, literal_terms)
            overflow.extend(ordered[_MAX_NOTES_PER_POSITION:])
            buckets[position] = ordered[:_MAX_NOTES_PER_POSITION]

    for position in ("top", "middle", "base"):
        while len(buckets[position]) < _MIN_NOTES_PER_POSITION and overflow:
            buckets[position].append(overflow.pop(0))
    for position in ("top", "middle", "base"):
        while len(buckets[position]) < _MIN_NOTES_PER_POSITION:
            donors = sorted(
                (p for p in ("top", "middle", "base") if p != position and len(buckets[p]) > _MIN_NOTES_PER_POSITION),
                key=lambda p: -len(buckets[p]),
            )
            if not donors:
                break
            buckets[position].append(buckets[donors[0]].pop())

    return buckets
