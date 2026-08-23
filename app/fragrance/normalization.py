"""Port of app/utils/fragranceNormalization.js -- shared normalization helpers so ingestion
scripts, scoring services, and the chat/AI layer all match text the same way.
"""

import re
import unicodedata

_NON_PRODUCT_CHARS = re.compile(r"[^a-z0-9\s]")
_WHITESPACE = re.compile(r"\s+")
_NON_REGION_CHARS = re.compile(r"[^a-z\s]")
_WORD = re.compile(r"[a-zA-Z]+")


def normalize_product_name(title: str | None) -> str:
    if not title:
        return ""
    lowered = unicodedata.normalize("NFC", str(title).lower())
    stripped = _NON_PRODUCT_CHARS.sub("", lowered)
    return _WHITESPACE.sub(" ", stripped).strip()


# The raw "Updated Season" column is inconsistently labeled in the real data ("Fall" and "Autumn
# Months" both exist as distinct values) -- an exact match on just the clean season name would
# silently miss the alias-labeled rows.
SEASON_ALIASES = {
    "Winter": ["Winter", "Winter Months"],
    "Spring": ["Spring", "Spring Months"],
    "Summer": ["Summer", "Summer Months"],
    "Fall": ["Fall", "Autumn Months"],
}


def normalize_region_text(text: str) -> str:
    stripped = _NON_REGION_CHARS.sub("", text.lower())
    return _WHITESPACE.sub(" ", stripped).strip()


# A small, explicit, human-reviewed list of unambiguous location misspellings -- deliberately NOT
# a fuzzy-match/edit-distance heuristic, since that can silently "correct" one real place into a
# different real place.
_KNOWN_LOCATION_CORRECTIONS = {
    "los angelos": "Los Angeles",
    "califronia": "California",
    "calfornia": "California",
    "united state": "United States",
    "unitedstates": "United States",
    "untied states": "United States",
    "newyork": "New York",
    "phillipines": "Philippines",
    "phillippines": "Philippines",
}


def resolve_location_input(input_text: str | None, region_maps: dict[str, dict[str, str]] | None) -> dict:
    """region_maps: {"city"|"stateName"|"countryName": {normalized -> real casing}}"""
    if not input_text or not input_text.strip():
        return {"resolved": False, "needsConfirmation": False, "value": None}

    normalized = normalize_region_text(input_text)
    known_correction = _KNOWN_LOCATION_CORRECTIONS.get(normalized)

    candidate_text = known_correction or input_text
    candidate_normalized = normalize_region_text(candidate_text)

    for field in ("city", "stateName", "countryName"):
        region_map = (region_maps or {}).get(field)
        if region_map and candidate_normalized in region_map:
            return {
                "resolved": True,
                "needsConfirmation": False,
                "field": field,
                "value": region_map[candidate_normalized],
                "wasCorrected": bool(known_correction),
                "originalInput": input_text,
            }

    if known_correction:
        return {
            "resolved": True,
            "needsConfirmation": False,
            "field": None,
            "value": known_correction,
            "wasCorrected": True,
            "originalInput": input_text,
        }

    return {"resolved": False, "needsConfirmation": True, "value": None, "originalInput": input_text}


# A small, explicit, human-reviewed list of common misspellings of the fragrance vocabulary this
# engine actually matches against -- deliberately NOT a fuzzy/edit-distance corrector, for the same
# reason as the location list above.
_PREFERENCE_VOCABULARY_CORRECTIONS = {
    "spricy": "spicy",
    "spicey": "spicy",
    "gourmant": "gourmand",
    "gourmound": "gourmand",
    "fruty": "fruity",
    "fruitty": "fruity",
    "frutiy": "fruity",
    "aquitic": "aquatic",
    "aquatik": "aquatic",
    "freash": "fresh",
    "floreal": "floral",
    "florel": "floral",
    "woddy": "woody",
    "woddey": "woody",
    "vanila": "vanilla",
    "vannila": "vanilla",
    "citris": "citrus",
    "citrous": "citrus",
    "smokey": "smoky",
    "aromattic": "aromatic",
    "relaxin": "relaxing",
    "relaxeing": "relaxing",
    "powdary": "powdery",
}


def correct_preference_vocabulary(text: str | None) -> dict:
    if not text or not isinstance(text, str):
        return {"corrected": text or "", "corrections": []}

    corrections = []

    def _replace(match: re.Match) -> str:
        word = match.group(0)
        fix = _PREFERENCE_VOCABULARY_CORRECTIONS.get(word.lower())
        if not fix:
            return word
        corrections.append({"original": word, "corrected": fix})
        if word == word.upper():
            return fix.upper()
        if word[0] == word[0].upper():
            return fix[0].upper() + fix[1:]
        return fix

    corrected = _WORD.sub(_replace, text)
    return {"corrected": corrected, "corrections": corrections}


def correct_preference_vocabulary_list(items: list) -> dict:
    corrected = []
    corrections = []
    for item in items or []:
        if not isinstance(item, str):
            corrected.append(item)
            continue
        result = correct_preference_vocabulary(item)
        corrected.append(result["corrected"])
        corrections.extend(result["corrections"])
    return {"corrected": corrected, "corrections": corrections}
