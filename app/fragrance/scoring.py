"""Port of app/utils/fragranceScoring.js -- Phase 3 base scoring rules (verbatim point values)."""

from app.fragrance.compatibility import PREFERENCE_FAMILIES, detect_families

SCORE_WEIGHTS = {
    "sameCity": 5,
    "sameCountry": 4,
    "sameStateRegionOrClimate": 3,
    "sameSeason": 4,
    "matchesLike": 5,
    "conflictsDislike": -10,
    "repeatPurchaseBySimilarCustomer": 3,
    "popularAmongSimilarCustomers": 2,
}

# Spec: "Do not automatically reject a product for one minor supporting note unless the conflict is
# substantial." Severity scales with how many notes hit a disliked family, and whether a hit falls
# among the first few notes (more prominent, since notesJson preserves source spreadsheet order).
_PROMINENT_NOTE_WINDOW = 3


def classify_dislike_conflict(product_notes: list[str] | None, dislike_families: list[str] | None) -> dict:
    notes = product_notes or []
    if not notes or not dislike_families:
        return {"severity": "none", "matchedFamilies": [], "matchedNoteCount": 0}

    matched_families = [
        family for family in dislike_families
        if (keywords := PREFERENCE_FAMILIES.get(family)) and detect_families(notes, {family: keywords})
    ]
    if not matched_families:
        return {"severity": "none", "matchedFamilies": [], "matchedNoteCount": 0}

    lower_notes = [str(n).lower() for n in notes]
    matched_note_count = 0
    matched_in_prominent_window = False
    for family in matched_families:
        keywords = PREFERENCE_FAMILIES[family]
        for index, note in enumerate(lower_notes):
            if any(kw in note for kw in keywords):
                matched_note_count += 1
                if index < _PROMINENT_NOTE_WINDOW:
                    matched_in_prominent_window = True

    match_ratio = matched_note_count / len(notes)
    if matched_note_count >= 3 or match_ratio >= 0.4:
        severity = "high"
    elif matched_note_count == 2 or (matched_note_count == 1 and matched_in_prominent_window):
        severity = "medium"
    else:
        severity = "low"

    return {"severity": severity, "matchedFamilies": matched_families, "matchedNoteCount": matched_note_count}


def matched_likes(product_notes: list[str] | None, like_families: list[str]) -> list[str]:
    return [
        family for family in like_families
        if (keywords := PREFERENCE_FAMILIES.get(family)) and detect_families(product_notes, {family: keywords})
    ]


def like_match_strength(notes: list[str] | None, family: str) -> float:
    keywords = PREFERENCE_FAMILIES.get(family)
    if not keywords or not notes:
        return 0
    matching_count = sum(1 for note in notes if any(kw in str(note).lower() for kw in keywords))
    return matching_count / len(notes)


def compute_evidence_level(distinct_similar_customers: int = 0, same_season_orders: int = 0) -> str:
    if distinct_similar_customers >= 10 or same_season_orders >= 25:
        return "high"
    if distinct_similar_customers >= 3 or same_season_orders >= 5:
        return "medium"
    return "low"
