"""Port of app/utils/fragranceCompatibility.js -- the deterministic family/note vocabulary and
Phase-6 compatibility/risk logic. Every keyword list is matched as a case-insensitive substring
against a product's real notes -- never used to infer notes a product doesn't actually have.

Ported faithfully, including the documented "known limitations" (bare "rose" matching inside
"Rosemary", etc.) -- see the inline comments carried over from the JS source for the reasoning.
"""

import re
from typing import Callable

Product = dict  # {"title": str, "notes": list[str]}

# ============================================================
# PREFERENCE_FAMILIES / COMPATIBILITY_TAGS
# ============================================================

PREFERENCE_FAMILIES: dict[str, list[str]] = {
    "fruity": [
        "fruity", "mango", "pineapple", "pear", "apple", "berr", "strawberry", "peach",
        "apricot", "guava", "black currant", "blackcurrant", "fig", "coconut",
    ],
    "sweet": [
        "sweet", "vanilla", "sugar", "marshmallow", "cotton candy", "candy", "caramel",
        "honey", "tonka", "whipped cream",
    ],
    "fresh": [
        "fresh", "citrus", "bergamot", "lemon", "lime", "mandarin", "grapefruit",
        "orange", "tangerine", "aquatic", "marine", "green", "mint", "neroli", "aromatic",
    ],
    "spicy": [
        "spicy", "spice", "pepper", "saffron", "clove", "cinnamon", "cardamom", "coriander",
        "ginger", "nutmeg", "cumin",
    ],
    "strongHeavy": [
        "strong", "heavy", "oud", "agarwood", "smoke", "smoky", "leather", "amber",
        "tobacco", "resin", "incense",
    ],
    "woody": ["woody", "wood", "sandalwood", "cedar", "vetiver", "patchouli", "guaiac"],
    "musk": ["musk", "musky"],
    "powdery": ["powdery", "orris", "iris", "violet", "heliotrope", "powder"],
    "floral": [
        "floral", "flower",
        "jasmine", "rose", "tuberose", "magnolia", "freesia", "gardenia", "peony",
        "ylang", "ylang-ylang", "osmanthus", "orange blossom",
        "lily", "lily of the valley", "lily-of-the-valley", "lilac", "orchid", "carnation",
        "narcissus", "wisteria", "hyacinth", "jonquil", "frangipani", "plumeria", "champaca", "muguet",
        "camellia", "azalea", "cyclamen", "lotus", "mimosa",
        "geranium", "hedione",
        "violet", "iris", "orris",
    ],
    "dry": ["dry", "vetiver", "oakmoss", "papyrus", "cedar", "dry wood", "black tea", "tobacco leaf", "birch", "mineral"],
    "earthy": ["earthy", "earth", "vetiver", "oakmoss", "patchouli", "moss", "soil", "cypriol", "nagarmotha", "papyrus", "galbanum", "angelica", "mushroom", "forest floor"],
    "natural": ["natural", "herb", "sage", "basil", "rosemary", "green tea", "black tea", "tea leaf", "sea salt", "moss", "vetiver", "green note", "fir", "cypress", "cedar", "botanical"],
}

_ALMOND_POWDERY_CONTEXT = ["orris", "iris", "violet", "heliotrope"]
_ALMOND_GOURMAND_CONTEXT = ["vanilla", "tonka", "caramel", "honey"]


def classify_almond_character(notes: list[str] | None) -> str | None:
    note_text = " | ".join(notes or []).lower()
    if "almond" not in note_text:
        return None
    if any(kw in note_text for kw in _ALMOND_POWDERY_CONTEXT):
        return "powdery"
    if any(kw in note_text for kw in _ALMOND_GOURMAND_CONTEXT):
        return "gourmand"
    return "nutty"


COMPATIBILITY_TAGS: dict[str, list[str]] = {
    "citrus": ["citrus", "bergamot", "lemon", "lime", "mandarin", "grapefruit", "orange", "tangerine"],
    "musk": ["musk"],
    "vanilla": ["vanilla"],
    "aquatic": ["aquatic", "marine"],
    "woody": ["wood", "sandalwood", "cedar", "vetiver", "patchouli", "guaiac"],
    "aromatic": ["aromatic", "lavender", "rosemary", "sage", "basil", "thyme"],
    "smoky": ["smoke", "smoky", "incense", "oud", "agarwood"],
    "amber": ["amber", "ambergris", "ambroxan", "labdanum"],
}


def detect_families(notes: list[str] | None, family_map: dict[str, list[str]]) -> list[str]:
    note_text = " | ".join(notes or []).lower()
    if not note_text:
        return []
    return [family for family, keywords in family_map.items() if any(kw in note_text for kw in keywords)]


def text_to_preference_families(strings: list[str] | None) -> list[str]:
    families: list[str] = []
    seen = set()
    for s in strings or []:
        if not s:
            continue
        for family in detect_families([s], PREFERENCE_FAMILIES):
            if family not in seen:
                seen.add(family)
                families.append(family)
    return families


# Family keywords that are themselves just a generic style/descriptor word, or a deliberately
# truncated catch-all fragment, rather than one specific real note -- excluded from literal-note
# extraction so "Fruity"/"Fresh"/"Citrus" don't count as literal note terms, only "Apple"/"Bergamot".
FAMILY_DESCRIPTOR_WORDS = {
    "fruity", "sweet", "fresh", "spicy", "spice", "strong", "heavy", "woody", "musk", "musky", "powdery",
    "berr", "citrus", "aquatic", "marine", "green", "aromatic",
    "dry", "earthy", "natural", "earth", "soil", "herb", "mineral", "green note", "forest floor", "botanical", "dry wood",
    "floral", "flower",
}


def contains_whole_word(text: str, term: str) -> bool:
    return re.search(r"\b" + re.escape(term) + r"\b", text, re.IGNORECASE) is not None


def literal_note_terms_from_likes(strings: list[str] | None) -> list[str]:
    terms: list[str] = []
    seen = set()
    for s in strings or []:
        if not s:
            continue
        trimmed = str(s).strip()
        if not trimmed:
            continue
        for keywords in PREFERENCE_FAMILIES.values():
            for kw in keywords:
                if kw not in FAMILY_DESCRIPTOR_WORDS and contains_whole_word(trimmed, kw) and kw not in seen:
                    seen.add(kw)
                    terms.append(kw)
    return terms


def split_dislikes_by_exactness(dislikes: list[str] | None) -> dict:
    """Exact-note dislike ("I dislike Sandalwood") -> hard-exclude that note only, never the whole
    family. Bare family/style dislike ("Woody fragrances") -> keeps the existing severity-scaled
    classifyDislikeConflict treatment.
    """
    exact_note_dislikes: list[str] = []
    explicit_family_dislikes: list[str] = []
    seen_notes: set[str] = set()
    seen_families: set[str] = set()
    for d in dislikes or []:
        if not d:
            continue
        literal_terms = literal_note_terms_from_likes([d])
        if literal_terms:
            for t in literal_terms:
                if t not in seen_notes:
                    seen_notes.add(t)
                    exact_note_dislikes.append(t)
        else:
            for f in text_to_preference_families([d]):
                if f not in seen_families:
                    seen_families.add(f)
                    explicit_family_dislikes.append(f)
    return {"exactNoteDislikes": exact_note_dislikes, "explicitFamilyDislikes": explicit_family_dislikes}


def matched_real_notes_in_text(text: str | None, real_notes: list[str] | None) -> list[str]:
    if not text or not real_notes:
        return []
    seen = set()
    matches = []
    for note in real_notes:
        trimmed = str(note or "").strip()
        if not trimmed:
            continue
        key = trimmed.lower()
        if key in seen or key in FAMILY_DESCRIPTOR_WORDS or not contains_whole_word(text, trimmed):
            continue
        seen.add(key)
        matches.append(key)
    return matches


def literal_note_match_count(notes: list[str] | None, literal_terms: list[str] | None) -> int:
    if not literal_terms or not notes:
        return 0
    note_text = " | ".join(notes)
    return sum(1 for term in literal_terms if contains_whole_word(note_text, term))


def matched_literal_terms(notes: list[str] | None, literal_terms: list[str] | None) -> list[str]:
    if not literal_terms or not notes:
        return []
    note_text = " | ".join(notes)
    return [term for term in literal_terms if contains_whole_word(note_text, term)]


def missing_literal_terms(notes: list[str] | None, literal_terms: list[str] | None) -> list[str]:
    if not literal_terms:
        return []
    note_text = " | ".join(notes or [])
    return [term for term in literal_terms if not contains_whole_word(note_text, term)]


EXACT_NOTE_COVERAGE_TIERS = [10, 7, 5]


def exact_note_coverage_score(distinct_match_count: int) -> int:
    score = 0
    for i in range(distinct_match_count):
        score += EXACT_NOTE_COVERAGE_TIERS[min(i, len(EXACT_NOTE_COVERAGE_TIERS) - 1)]
    return score


FAMILY_BREADTH_BONUS_FROM_SECOND = [6, 3]


def family_breadth_coverage_score(distinct_family_count: int) -> int:
    score = 0
    for i in range(1, distinct_family_count):
        score += FAMILY_BREADTH_BONUS_FROM_SECOND[min(i - 1, len(FAMILY_BREADTH_BONUS_FROM_SECOND) - 1)]
    return score


# ============================================================
# Pair compatibility (Phase 6)
# ============================================================

COMPATIBLE_PAIRS = [
    ("fruity", "citrus"),
    ("fruity", "floral"),
    ("fruity", "musk"),
    ("fruity", "vanilla"),
    ("sweet", "citrus"),
    ("sweet", "vanilla"),
    ("aquatic", "woody"),
    ("floral", "musk"),
    ("citrus", "aromatic"),
    ("smoky", "amber"),
    ("woody", "amber"),
    ("aquatic", "spicy"),
    ("citrus", "spicy"),
    ("fresh", "spicy"),
    ("vanilla", "spicy"),
    ("woody", "citrus"),
    ("aromatic", "spicy"),
    ("fruity", "spicy"),
]


def pair_is_compatible(family_a: str, family_b: str) -> bool:
    return any((a == family_a and b == family_b) or (a == family_b and b == family_a) for a, b in COMPATIBLE_PAIRS)


def _find_duplicated_direction(products: list[Product]) -> str | None:
    direction_counts: dict[str, int] = {}
    for p in products:
        for family in detect_families(p.get("notes"), PREFERENCE_FAMILIES):
            direction_counts[family] = direction_counts.get(family, 0) + 1
    for family, count in direction_counts.items():
        if count == len(products) and len(products) > 1:
            return family
    return None


def _max_pairwise_note_overlap(products: list[Product]) -> float:
    max_overlap = 0.0
    for i in range(len(products)):
        for j in range(i + 1, len(products)):
            set_a = {str(n).lower() for n in (products[i].get("notes") or [])}
            set_b = {str(n).lower() for n in (products[j].get("notes") or [])}
            if not set_a or not set_b:
                continue
            overlap = len(set_a & set_b) / min(len(set_a), len(set_b))
            if overlap > max_overlap:
                max_overlap = overlap
    return max_overlap


NOTABLE_OVERLAP_RATIO = 0.35


# ============================================================
# Risk rules (Phase 6)
# ============================================================


def _rule_excessive_gourmand_heat(products, context):
    if context.get("season") != "Summer":
        return None
    gourmand_keywords = ["sugar", "caramel", "marshmallow", "honey"]
    hits = [p for p in products if any(kw in " ".join(p.get("notes") or []).lower() for kw in gourmand_keywords)]
    return (
        "Multiple heavy sugar/caramel/marshmallow/honey notes may feel too heavy in summer heat"
        if len(hits) >= 2 else None
    )


def _rule_multiple_heavy_components(products, context):
    heavy = [p for p in products if "strongHeavy" in detect_families(p.get("notes"), PREFERENCE_FAMILIES)]
    return "Multiple heavy oud/leather/smoke/tobacco/resin components may overwhelm the blend" if len(heavy) >= 2 else None


def _rule_competing_fruits(products, context):
    fruity = [p for p in products if "fruity" in detect_families(p.get("notes"), PREFERENCE_FAMILIES)]
    return "Several strongly fruity products may compete rather than layer cleanly" if len(fruity) >= 3 else None


def _rule_spice_conflict(products, context):
    spicy = [p for p in products if "spicy" in detect_families(p.get("notes"), PREFERENCE_FAMILIES)]
    return "Multiple spicy products may clash rather than complement" if len(spicy) >= 2 else None


def _rule_citrus_smoke_clash(products, context):
    has_citrus = any("citrus" in detect_families(p.get("notes"), COMPATIBILITY_TAGS) for p in products)
    has_smoke = any("smoky" in detect_families(p.get("notes"), COMPATIBILITY_TAGS) for p in products)
    return "Sharp citrus alongside dense smoky notes can clash without a bridging note" if has_citrus and has_smoke else None


def _rule_quadbrid_complexity(products, context):
    return "Four products in one blend raises the risk of a muddled, over-complex result" if len(products) >= 4 else None


def _rule_powdery_overload(products, context):
    powdery = [p for p in products if "powdery" in detect_families(p.get("notes"), PREFERENCE_FAMILIES)]
    return (
        "Multiple powdery orris/iris/violet/heliotrope notes may build into a heavy, cosmetic-powder impression"
        if len(powdery) >= 2 else None
    )


def _rule_duplicate_direction(products, context):
    family = _find_duplicated_direction(products)
    if not family:
        return None
    if family in (context.get("likeFamilies") or []):
        return None
    return f'Every product shares the same "{family}" direction with no contrasting role'


def _rule_single_family_concentration(products, context):
    family = _find_duplicated_direction(products)
    if not family:
        return None
    if family not in (context.get("likeFamilies") or []):
        return None

    roled_products = context.get("roledProducts")
    roles = roled_products if roled_products and len(roled_products) == len(products) else products
    distinct_roles = {p.get("role") for p in roles if p.get("role")}
    has_role_diversity = len(distinct_roles) >= 2

    note_text = " | ".join(n for p in products for n in (p.get("notes") or [])).lower()
    gourmand_overload = sum(1 for kw in ["sugar", "caramel", "marshmallow", "honey"] if kw in note_text) >= 2
    heavy_overload = sum(1 for p in products if "strongHeavy" in detect_families(p.get("notes"), PREFERENCE_FAMILIES)) >= 2
    hot_weather_conflict = context.get("season") == "Summer" and (gourmand_overload or heavy_overload)
    strength_conflict = context.get("strengthPreference") == "light" and heavy_overload
    notable_overlap = _max_pairwise_note_overlap(products) >= NOTABLE_OVERLAP_RATIO
    too_complex_for_role_diversity = len(products) >= 4 and not has_role_diversity

    problems = [
        p for p in [
            "a hot-weather conflict" if hot_weather_conflict else None,
            "more strength than your light-strength preference" if strength_conflict else None,
            "notably overlapping components" if notable_overlap else None,
            "too much complexity for the role diversity present" if too_complex_for_role_diversity else None,
            "excessive sweetness stacked with excessive heaviness" if (gourmand_overload and heavy_overload) else None,
        ] if p
    ]

    if problems:
        severity = "high" if hot_weather_conflict else "medium"
        return {
            "id": "excessive_direction_stacking",
            "severity": severity,
            "message": f'Repeated "{family}" direction (your own stated preference) compounds into a real problem: {", ".join(problems)}',
        }
    if not has_role_diversity:
        return {
            "id": "insufficient_role_diversity",
            "severity": "low",
            "message": f'Every product leans "{family}" — your own stated preference — with limited role diversity, though nothing else conflicts',
        }
    return {
        "id": "single_family_concentration",
        "severity": "advisory",
        "message": f'Every product shares your own stated "{family}" preference, with distinct roles and no real conflict',
    }


RISK_RULES: list[dict] = [
    {"id": "excessive_gourmand_heat", "severity": "medium", "check": _rule_excessive_gourmand_heat},
    {"id": "multiple_heavy_components", "severity": "medium", "check": _rule_multiple_heavy_components},
    {"id": "competing_fruits", "severity": "low", "check": _rule_competing_fruits},
    {"id": "spice_conflict", "severity": "low", "check": _rule_spice_conflict},
    {"id": "citrus_smoke_clash", "severity": "advisory", "check": _rule_citrus_smoke_clash},
    {"id": "quadbrid_complexity", "severity": "advisory", "check": _rule_quadbrid_complexity},
    {"id": "powdery_overload", "severity": "medium", "check": _rule_powdery_overload},
    {"id": "duplicate_direction", "severity": "medium", "check": _rule_duplicate_direction},
    {"id": "single_family_concentration", "severity": "advisory", "check": _rule_single_family_concentration},
]


def _message_of(result) -> str | None:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return result.get("message")
    return None


def assess_combination_risks(products: list[Product], context: dict | None = None) -> list[str]:
    context = context or {}
    return [msg for rule in RISK_RULES if (msg := _message_of(rule["check"](products, context)))]


RISK_SEVERITY_PENALTY = {"advisory": -1, "low": -2, "medium": -5, "high": -10, "critical": -10}
SEVERITY_RANK = {"advisory": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
FAMILY_SPECIFIC_RISK_FAMILY = {
    "multiple_heavy_components": "strongHeavy",
    "competing_fruits": "fruity",
    "spice_conflict": "spicy",
    "powdery_overload": "powdery",
}
DUPLICATED_FAMILY_RISK_IDS = {
    "duplicate_direction", "single_family_concentration", "insufficient_role_diversity", "excessive_direction_stacking",
}


def group_and_penalize_risks(
    hits: list[dict], correlation_key_for: Callable[[dict], str] | None = None
) -> dict:
    correlation_key_for = correlation_key_for or (lambda hit: hit["id"])
    groups: dict[str, dict] = {}
    for hit in hits:
        key = correlation_key_for(hit)
        existing = groups.get(key)
        if not existing or SEVERITY_RANK[hit["severity"]] > SEVERITY_RANK[existing["severity"]]:
            groups[key] = hit
    counted_ids = {h["id"] for h in groups.values()}
    breakdown = [
        {
            "id": hit["id"],
            "message": hit["message"],
            "severity": hit["severity"],
            "counted": hit["id"] in counted_ids,
            "penalty": RISK_SEVERITY_PENALTY[hit["severity"]] if hit["id"] in counted_ids else 0,
        }
        for hit in hits
    ]
    has_critical = any(h["severity"] == "critical" for h in hits)
    risk_penalty = sum(r["penalty"] for r in breakdown)
    return {"breakdown": breakdown, "riskPenalty": risk_penalty, "hasCritical": has_critical}


def assess_combination_risk_details(products: list[Product], context: dict | None = None) -> dict:
    context = context or {}
    hits = []
    for rule in RISK_RULES:
        result = rule["check"](products, context)
        if not result:
            continue
        if isinstance(result, dict):
            hits.append({
                "id": result.get("id") or rule["id"],
                "message": result["message"],
                "severity": result.get("severity") or rule["severity"],
            })
        else:
            hits.append({"id": rule["id"], "message": result, "severity": rule["severity"]})

    duplicated_family = _find_duplicated_direction(products)

    def correlation_key_for(hit: dict) -> str:
        if hit["id"] in DUPLICATED_FAMILY_RISK_IDS:
            return f"family:{duplicated_family}" if duplicated_family else hit["id"]
        if hit["id"] in FAMILY_SPECIFIC_RISK_FAMILY:
            return f"family:{FAMILY_SPECIFIC_RISK_FAMILY[hit['id']]}"
        return hit["id"]

    return group_and_penalize_risks(hits, correlation_key_for)


# ============================================================
# Deterministic natural-language preference interpretation
# ============================================================

_SENSITIVITY_PHRASES = [
    re.compile(r"hit(s)?\s*my\s*nose", re.IGNORECASE),
    re.compile(r"headache", re.IGNORECASE),
    re.compile(r"migraine", re.IGNORECASE),
    re.compile(r"\bsharp\b", re.IGNORECASE),
    re.compile(r"\bpiercing\b", re.IGNORECASE),
    re.compile(r"\bharsh\b", re.IGNORECASE),
    re.compile(r"too\s*strong", re.IGNORECASE),
    re.compile(r"\bstrong\s*(scent|smell|perfume|fragrance|cologne)", re.IGNORECASE),
    re.compile(r"overpowering", re.IGNORECASE),
    re.compile(r"suffocat", re.IGNORECASE),
    re.compile(r"\bheavy\b", re.IGNORECASE),
    re.compile(r"haddik", re.IGNORECASE),
    re.compile(r"uncomfortable", re.IGNORECASE),
    re.compile(r"cannot tolerate|can'?t tolerate", re.IGNORECASE),
    re.compile(r"sensitive (nose|to (scent|smell|fragrance|perfume))", re.IGNORECASE),
]

DIRECTION_RISK_NOTES = {
    "pepper-heavy": ["black pepper", "pink pepper", "pepper"],
    "dense-spicy": ["saffron", "cinnamon", "cumin", "clove"],
    "smoky": ["smoke", "incense"],
    "oud": ["oud", "agarwood"],
    "leather": ["leather"],
    "tobacco": ["tobacco"],
    "heavy-amber": ["labdanum", "ambergris", "ambroxan", "amberwood", "amber"],
    "resinous": ["resin"],
    "dense-patchouli": ["patchouli"],
    "strong-guaiac": ["guaiac"],
}

SOFTENING_NOTE_KEYWORDS = [
    "lavender", "sandalwood", "vanilla", "musk", "chamomile", "sea salt", "tea", "aloe",
    "white musk", "clean musk", "linen", "cotton", "powder", "iris", "neroli", "mint",
]

_STYLE_DIRECTION_MAP = [
    {
        "pattern": re.compile(r"relax|calm|soothing|gentle|soft|easy|comfort", re.IGNORECASE),
        "preferredDirections": ["relaxing", "airy", "clean", "watery", "green-tea", "soft-musky", "light-fruity"],
    },
    {
        "pattern": re.compile(r"energetic|active|sport|bold|confident|invigorat", re.IGNORECASE),
        "preferredDirections": ["energetic", "crisp", "citrus-forward", "bright"],
    },
    {
        "pattern": re.compile(r"elegant|sophisticat|formal|professional", re.IGNORECASE),
        "preferredDirections": ["refined", "polished", "understated"],
    },
    {
        "pattern": re.compile(r"playful|fun\b", re.IGNORECASE),
        "preferredDirections": ["playful", "sweet", "gourmand-light"],
    },
]

BASE_SENSITIVITY_AVOID_DIRECTIONS = [
    "sharp", "piercing", "pepper-heavy", "dense-spicy", "smoky", "heavy-amber",
    "resinous", "oud", "leather", "tobacco", "overly-complex",
]


def interpret_customer_preferences(profile: dict | None) -> dict:
    profile = profile or {}
    additional_preferences = profile.get("additionalPreferences")
    additional_text = (
        " . ".join(additional_preferences) if isinstance(additional_preferences, list) else (additional_preferences or "")
    )
    text_blob = " . ".join(
        p for p in [
            *(profile.get("dislikes") or []),
            additional_text,
            profile.get("preferredStyle"),
            profile.get("inferredStyle"),
        ] if p
    )

    sensitivity_hit = any(pattern.search(text_blob) for pattern in _SENSITIVITY_PHRASES)
    style_match = next((s for s in _STYLE_DIRECTION_MAP if s["pattern"].search(text_blob)), None)

    return {
        "preferredDirections": list(style_match["preferredDirections"]) if style_match else [],
        "avoidedDirections": list(BASE_SENSITIVITY_AVOID_DIRECTIONS) if sensitivity_hit else [],
        "strengthPreference": "light" if sensitivity_hit else (profile.get("strengthPreference") or None),
        "sensitivityLevel": "high" if sensitivity_hit else "none",
        "preferSimpleCombinations": sensitivity_hit,
        "preferredCombinationTypes": ["HYBRID"] if sensitivity_hit else [],
    }


def compute_complexity_level(note_count: int) -> str:
    if note_count <= 6:
        return "low"
    if note_count <= 12:
        return "moderate"
    if note_count <= 20:
        return "high"
    return "very-high"


def detect_intensity_drivers(notes: list[str] | None) -> list[str]:
    note_text = " | ".join(notes or []).lower()
    hits = []
    for keywords in DIRECTION_RISK_NOTES.values():
        for kw in keywords:
            if kw in note_text and kw not in hits:
                hits.append(kw)
    return hits


def detect_softening_notes(notes: list[str] | None) -> list[str]:
    note_text = " | ".join(notes or []).lower()
    return [kw for kw in SOFTENING_NOTE_KEYWORDS if kw in note_text]


INTENSITY_DRIVER_HARD_LIMIT = 3


def passes_intensity_filter(notes: list[str] | None, preference_intent: dict | None) -> bool:
    if not preference_intent or preference_intent.get("sensitivityLevel") != "high":
        return True
    if compute_complexity_level(len(notes or [])) == "very-high":
        return False
    return len(detect_intensity_drivers(notes)) < INTENSITY_DRIVER_HARD_LIMIT


PREFERRED_DIRECTION_MATCHERS = {
    "relaxing": ["lavender", "chamomile", "tea", "musk"],
    "airy": ["aquatic", "marine", "citrus", "green"],
    "clean": ["musk", "clean", "linen", "cotton", "soap"],
    "watery": ["aquatic", "marine", "sea salt", "water"],
    "green-tea": ["tea", "green"],
    "soft-musky": ["musk"],
    "light-fruity": ["fruity", "pear", "apple", "berr", "peach", "mango", "pineapple"],
    "energetic": ["citrus", "mint", "bergamot"],
    "crisp": ["citrus", "aquatic", "green"],
    "citrus-forward": ["citrus", "bergamot", "lemon", "orange"],
    "bright": ["citrus", "fruity"],
    "refined": ["musk", "iris", "sandalwood"],
    "polished": ["musk", "sandalwood"],
    "understated": ["musk", "clean"],
    "playful": ["fruity", "sweet"],
    "sweet": ["sweet", "vanilla"],
    "gourmand-light": ["vanilla", "sugar"],
}


def count_preferred_direction_matches(notes: list[str] | None, preferred_directions: list[str] | None) -> int:
    if not preferred_directions:
        return 0
    note_text = " | ".join(notes or []).lower()
    if not note_text:
        return 0
    count = 0
    for direction in preferred_directions:
        keywords = PREFERRED_DIRECTION_MATCHERS.get(direction)
        if keywords and any(kw in note_text for kw in keywords):
            count += 1
    return count


# ============================================================
# Lifestyle / occasion context interpretation
# ============================================================

_LIFESTYLE_PATTERNS = [
    {
        "name": "office",
        "pattern": re.compile(r"\boffice\b|\bofficer\b|\bwork(ing)?\b|\bprofessional\b|\bworkplace\b|\bmeeting\b|\bcorporate\b", re.IGNORECASE),
        "preferredDirections": ["clean", "polished", "understated", "refined"],
        "avoidedDirections": ["smoky", "dense-spicy", "heavy-amber"],
    },
    {
        "name": "gym",
        "pattern": re.compile(r"\bgym\b|\bworkout\b|\bwork\s*out\b|\bexercis(e|ing)\b|\bactive\b|\bsport(s)?\b|\btraining\b|\brunning\b", re.IGNORECASE),
        "preferredDirections": ["airy", "crisp", "citrus-forward", "watery"],
        "avoidedDirections": ["heavy-amber", "resinous", "dense-spicy", "smoky"],
    },
    {
        "name": "relaxation",
        "pattern": re.compile(r"\brelax|\bcalm|\bunwind|\bsoothing\b|\bgentle\b|\beasy\b|\bcomfort\b|\bcozy\b", re.IGNORECASE),
        "preferredDirections": ["relaxing", "soft-musky", "green-tea"],
        "avoidedDirections": ["smoky", "pepper-heavy"],
    },
    {
        "name": "daytime",
        "pattern": re.compile(r"\bdaytime\b|\bday\s*wear\b|\bmorning\b|\bafternoon\b", re.IGNORECASE),
        "preferredDirections": ["airy", "light-fruity", "crisp"],
        "avoidedDirections": ["heavy-amber"],
    },
    {
        "name": "evening",
        "pattern": re.compile(r"\bevening\b|\bnight\s*(out|wear)?\b|\bdate\b|\bformal\b|\bdinner\b", re.IGNORECASE),
        "preferredDirections": ["refined", "playful"],
        "avoidedDirections": [],
    },
    {
        "name": "outdoor-heat",
        "pattern": re.compile(r"\boutdoor\b|\bbeach\b|\bsummer\s*heat\b|\bhot\s*weather\b|\bhumid\b", re.IGNORECASE),
        "preferredDirections": ["airy", "watery", "crisp"],
        "avoidedDirections": ["heavy-amber", "dense-spicy", "resinous"],
    },
    {
        "name": "special-event",
        "pattern": re.compile(r"\bwedding\b|\bspecial\s*event\b|\bcelebration\b|\bparty\b|\bgala\b", re.IGNORECASE),
        "preferredDirections": ["playful", "refined"],
        "avoidedDirections": [],
    },
]


def interpret_lifestyle_context(profile: dict | None) -> dict:
    profile = profile or {}
    additional_preferences = profile.get("additionalPreferences")
    additional_text = (
        " . ".join(additional_preferences) if isinstance(additional_preferences, list) else additional_preferences
    )
    text_blob = " . ".join(p for p in [profile.get("occasion"), additional_text] if p)

    matched = [lifestyle for lifestyle in _LIFESTYLE_PATTERNS if lifestyle["pattern"].search(text_blob)]
    if not matched:
        return {"lifestyles": [], "preferredDirections": {}, "avoidedDirections": {}}

    weight = 1 / len(matched)
    preferred_directions: dict[str, float] = {}
    avoided_directions: dict[str, float] = {}
    for lifestyle in matched:
        for d in lifestyle["preferredDirections"]:
            preferred_directions[d] = preferred_directions.get(d, 0) + weight
        for d in lifestyle["avoidedDirections"]:
            avoided_directions[d] = avoided_directions.get(d, 0) + weight
    return {
        "lifestyles": [l["name"] for l in matched],
        "preferredDirections": preferred_directions,
        "avoidedDirections": avoided_directions,
    }


def count_avoided_direction_matches(notes: list[str] | None, avoided_directions: list[str] | None) -> int:
    if not avoided_directions:
        return 0
    note_text = " | ".join(notes or []).lower()
    if not note_text:
        return 0
    count = 0
    for direction in avoided_directions:
        keywords = DIRECTION_RISK_NOTES.get(direction)
        if keywords and any(kw in note_text for kw in keywords):
            count += 1
    return count
