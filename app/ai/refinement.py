"""Port of app/tools/fragranceAgentTools.server.js's deriveRefinementAdjustments -- turns a
customer's refinement feedback ("make it sweeter", "remove the dark chocolate") into deterministic
profile-bias adjustments, without waiting on an LLM to decide direction.
"""

import re

from app.fragrance.compatibility import literal_note_terms_from_likes, matched_real_notes_in_text, text_to_preference_families

_REFINEMENT_TYPE_KEYWORDS = [
    (re.compile(r"\bhybrid\b", re.IGNORECASE), ["HYBRID"]),
    (re.compile(r"\btribrid\b", re.IGNORECASE), ["TRIBRID"]),
    (re.compile(r"\bquadbrid\b", re.IGNORECASE), ["QUADBRID"]),
]

_REFINEMENT_FAMILY_KEYWORDS = [
    (re.compile(r"\bfresh(er)?\b", re.IGNORECASE), "Fresh"),
    (re.compile(r"\bsweet(er)?\b", re.IGNORECASE), "Sweet"),
    (re.compile(r"\bfruit(y|ier)?\b", re.IGNORECASE), "Fruity"),
    (re.compile(r"\bspic(y|ier|e)?\b", re.IGNORECASE), "Spicy"),
    (re.compile(r"\bwood(y|ier)?\b", re.IGNORECASE), "Woody"),
    (re.compile(r"\bmusk(y)?\b", re.IGNORECASE), "Musk"),
    (re.compile(r"\bpowder(y|ier)?\b", re.IGNORECASE), "Powdery"),
    (re.compile(r"\b(strong(er)?|heav(y|ier))\b", re.IGNORECASE), "Strong"),
]
_NEGATION_PATTERN = re.compile(r"\b(no|not|don'?t|without|remove|less|avoid|take out|excluding|get rid of)\b", re.IGNORECASE)
_POSITIVE_OVERRIDE_PATTERN = re.compile(r"\b(more|want|add|keep|prefer|love|like)\b", re.IGNORECASE)
_ONLY_NEW_PATTERN = re.compile(r"\bonly new\b|\bnew combinations? only\b", re.IGNORECASE)
_CLAUSE_SPLIT_PATTERN = re.compile(r"[,;]|\band\b|\bbut\b", re.IGNORECASE)


def _add(ordered: list[str], seen: set[str], value: str) -> None:
    if value not in seen:
        seen.add(value)
        ordered.append(value)


def derive_refinement_adjustments(feedback: str, current_notes: list[str] | None = None) -> dict:
    current_notes = current_notes or []
    clauses = [c.strip() for c in _CLAUSE_SPLIT_PATTERN.split(feedback) if c.strip()]

    add_likes: list[str] = []
    add_likes_seen: set[str] = set()
    add_dislikes: list[str] = []
    add_dislikes_seen: set[str] = set()
    add_like_terms: list[str] = []
    add_like_terms_seen: set[str] = set()
    add_dislike_terms: list[str] = []
    add_dislike_terms_seen: set[str] = set()

    polarity = "like"
    for clause in (clauses if clauses else [feedback]):
        if _NEGATION_PATTERN.search(clause):
            polarity = "dislike"
        elif _POSITIVE_OVERRIDE_PATTERN.search(clause):
            polarity = "like"

        target, target_seen = (add_dislikes, add_dislikes_seen) if polarity == "dislike" else (add_likes, add_likes_seen)
        target_terms, target_terms_seen = (add_dislike_terms, add_dislike_terms_seen) if polarity == "dislike" else (add_like_terms, add_like_terms_seen)

        clause_families: list[str] = []
        clause_families_seen: set[str] = set()
        for pattern, family in _REFINEMENT_FAMILY_KEYWORDS:
            if pattern.search(clause):
                _add(target, target_seen, family)
                _add(clause_families, clause_families_seen, family)
        for family in text_to_preference_families([clause]):
            _add(target, target_seen, family)
            _add(clause_families, clause_families_seen, family)

        literal_terms: list[str] = []
        literal_terms_seen: set[str] = set()
        for t in literal_note_terms_from_likes([clause]):
            _add(literal_terms, literal_terms_seen, t)
        for t in matched_real_notes_in_text(clause, current_notes):
            _add(literal_terms, literal_terms_seen, t)

        if literal_terms:
            for t in literal_terms:
                _add(target_terms, target_terms_seen, t)
        else:
            for f in clause_families:
                _add(target_terms, target_terms_seen, f)

    # A stated exclusion wins over an incidental positive mention of the same family elsewhere.
    add_likes = [f for f in add_likes if f not in add_dislikes_seen]
    add_like_terms = [t for t in add_like_terms if t not in add_dislike_terms_seen]

    type_match = next((types for pattern, types in _REFINEMENT_TYPE_KEYWORDS if pattern.search(feedback)), None)
    return {
        "addLikes": add_likes,
        "addDislikes": add_dislikes,
        "addLikeTerms": add_like_terms,
        "addDislikeTerms": add_dislike_terms,
        "allowedTypes": type_match,
        "onlyNew": bool(_ONLY_NEW_PATTERN.search(feedback)),
    }
