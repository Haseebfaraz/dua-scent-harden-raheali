"""Port of app/services/recommendationEngine.server.js (generate_new_product_combinations) --
builds NEW Hybrid/Tribrid/Quadbrid proposals from real catalog products, scores them on the
spec's compatibility dimensions, and computes deterministic mixing ratios. Never proposes a
combination already present in ExistingCombination, and never invents a product, note, or ratio.

Every proposal is split into internalProducts (real source titles/notes -- backend/Shopify only,
never customer-facing) and customerFacing* fields (generated deterministically from real data, no
source product name in any of them). validateCombinationShape runs on every proposal before it's
ever returned.
"""

import re
import time
from itertools import combinations
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ExistingCombination, FragranceProduct
from app.fragrance.combination_key import create_combination_key
from app.fragrance.compatibility import (
    COMPATIBILITY_TAGS,
    PREFERENCE_FAMILIES,
    assess_combination_risk_details,
    assess_combination_risks,
    count_avoided_direction_matches,
    count_preferred_direction_matches,
    compute_complexity_level,
    detect_families,
    detect_intensity_drivers,
    detect_softening_notes,
    exact_note_coverage_score,
    family_breadth_coverage_score,
    interpret_customer_preferences,
    interpret_lifestyle_context,
    literal_note_match_count,
    literal_note_terms_from_likes,
    matched_literal_terms,
    missing_literal_terms,
    pair_is_compatible,
    passes_intensity_filter,
    split_dislikes_by_exactness,
    text_to_preference_families,
)
from app.fragrance.normalization import normalize_product_name
from app.fragrance.scoring import SCORE_WEIGHTS, classify_dislike_conflict, matched_likes, like_match_strength
from app.fragrance.vocabulary import DIRECTION_VOCABULARY, _js_hash, describe_character, direction_for_role, pick_words
from app.fragrance.weather import has_season_weather_conflict
from app.services.copy_generation import apply_customer_facing_copy
from app.services.customer_profile import is_profile_ready_for_analysis

DEFAULT_MAX_RESULTS = 8
BOTTLE_ML = 34
COMPONENT_COUNT_BY_TYPE = {"HYBRID": 2, "TRIBRID": 3, "QUADBRID": 4}
ALL_TYPES = ["HYBRID", "TRIBRID", "QUADBRID"]
# Bounds on the combinatorial search -- top N order-history candidates act as "anchors", each
# paired with its own shortlist of the most compatible supporting products.
MAX_ANCHORS = 5
MAX_SUPPORT_SHORTLIST = 8

# ponytail: JS caches allProducts/allCombinations at module scope with a 5-minute TTL to avoid a
# full table scan on every tool call within one live conversation. Same shape here (process-global,
# not session-scoped -- these are near-static reference tables, ~3450/~432 rows).
_CATALOG_CACHE_TTL_SECONDS = 5 * 60
_catalog_cache: dict[str, Any] | None = None


async def _get_catalog_and_combinations(session: AsyncSession) -> dict[str, Any]:
    global _catalog_cache
    if _catalog_cache and (time.monotonic() - _catalog_cache["fetchedAt"]) < _CATALOG_CACHE_TTL_SECONDS:
        return _catalog_cache

    products = (
        await session.execute(
            select(FragranceProduct.title, FragranceProduct.normalizedTitle, FragranceProduct.notesJson, FragranceProduct.collection)
        )
    ).all()
    all_products = [
        {"title": t, "normalizedTitle": nt, "notesJson": nj if isinstance(nj, list) else [], "collection": c}
        for t, nt, nj, c in products
    ]
    combos = (
        await session.execute(
            select(
                ExistingCombination.title, ExistingCombination.normalizedTitle, ExistingCombination.type,
                ExistingCombination.componentProductsJson, ExistingCombination.tagLine, ExistingCombination.componentKey,
            )
        )
    ).all()
    all_combinations = [
        {"title": t, "normalizedTitle": nt, "type": ty, "componentProductsJson": cpj, "tagLine": tl, "componentKey": ck}
        for t, nt, ty, cpj, tl, ck in combos
    ]
    _catalog_cache = {"allProducts": all_products, "allCombinations": all_combinations, "fetchedAt": time.monotonic()}
    return _catalog_cache


ALL_FAMILIES = {**PREFERENCE_FAMILIES, **COMPATIBILITY_TAGS}


def _families_of(notes: list[str] | None) -> list[str]:
    return detect_families(notes, ALL_FAMILIES)


def has_hard_excluded_family(notes: list[str] | None, hard_exclude_families: list[str] | None) -> bool:
    if not hard_exclude_families:
        return False
    return any(f in hard_exclude_families for f in detect_families(notes, PREFERENCE_FAMILIES))


def has_hard_excluded_term(notes: list[str] | None, hard_exclude_terms: list[str] | None) -> bool:
    return literal_note_match_count(notes, hard_exclude_terms) > 0


def _is_hard_excluded(notes, hard_exclude_families, hard_exclude_terms) -> bool:
    return has_hard_excluded_family(notes, hard_exclude_families) or has_hard_excluded_term(notes, hard_exclude_terms)


def _note_overlap_ratio(notes_a: list[str] | None, notes_b: list[str] | None) -> float:
    set_a = {str(n).lower() for n in (notes_a or [])}
    set_b = {str(n).lower() for n in (notes_b or [])}
    if not set_a or not set_b:
        return 0
    overlap = len(set_a & set_b)
    return overlap / min(len(set_a), len(set_b))


NEAR_DUPLICATE_OVERLAP_RATIO = 0.5

MAX_PER_ANCHOR = 2
MAX_PRODUCT_APPEARANCES = 3


def _select_diverse_results(sorted_results: list[dict], maximum_results: int) -> list[dict]:
    accepted: list[dict] = []
    skipped: list[dict] = []
    anchor_counts: dict[str, int] = {}
    product_counts: dict[str, int] = {}

    for result in sorted_results:
        if len(accepted) >= maximum_results:
            break
        normalized_titles = [normalize_product_name(p["title"]) for p in result["internalProducts"]]
        anchor_title = normalized_titles[0]
        anchor_count = anchor_counts.get(anchor_title, 0)
        product_would_overflow = any(product_counts.get(t, 0) >= MAX_PRODUCT_APPEARANCES for t in normalized_titles)

        if anchor_count >= MAX_PER_ANCHOR or product_would_overflow:
            skipped.append(result)
            continue
        accepted.append(result)
        anchor_counts[anchor_title] = anchor_count + 1
        for t in normalized_titles:
            product_counts[t] = product_counts.get(t, 0) + 1

    for result in skipped:
        if len(accepted) >= maximum_results:
            break
        accepted.append(result)
    return accepted


FAMILY_ROLE_PRIORITY = [
    ("fresh", "Freshness"),
    ("fruity", "Main fruit body"),
    ("sweet", "Sweetness"),
    ("floral", "Floral bridge"),
    ("musk", "Musk/wood base"),
    ("woody", "Musk/wood base"),
    ("amber", "Musk/wood base"),
    ("strongHeavy", "Longevity support"),
]


def _dominant_role(notes: list[str] | None) -> str | None:
    note_list = notes or []
    counts_by_role: dict[str, int] = {}
    for family, role in FAMILY_ROLE_PRIORITY:
        keywords = ALL_FAMILIES.get(family)
        if not keywords:
            continue
        count = sum(1 for note in note_list if any(kw in str(note).lower() for kw in keywords))
        counts_by_role[role] = counts_by_role.get(role, 0) + count
    best_role, best_count = None, 0
    for role, count in counts_by_role.items():
        if count > best_count:
            best_count, best_role = count, role
    return best_role


def _floral_note_count_of(notes: list[str] | None) -> int:
    keywords = PREFERENCE_FAMILIES["floral"]
    return sum(1 for note in (notes or []) if any(kw in str(note).lower() for kw in keywords))


def assign_roles(combo_products: list[dict]) -> list[dict]:
    result = []
    for p in combo_products:
        families = _families_of(p.get("notes"))
        role = _dominant_role(p.get("notes"))
        primary_family = next((f for f, r in FAMILY_ROLE_PRIORITY if r == role), None) if role else None
        total_note_count = len(p.get("notes") or [])
        floral_note_count = _floral_note_count_of(p.get("notes"))
        result.append({
            **p,
            "role": role or "Contrast",
            "hasDetectedFamily": len(families) > 0,
            "secondaryRoles": [f for f in families if f != primary_family],
            "intensityDrivers": detect_intensity_drivers(p.get("notes")),
            "softeningNotes": detect_softening_notes(p.get("notes")),
            "complexityLevel": compute_complexity_level(len(p.get("notes") or [])),
            "floralNoteCount": floral_note_count,
            "totalNoteCount": total_note_count,
            "floralCoverage": (floral_note_count / total_note_count) if total_note_count > 0 else 0,
        })
    return result


def validate_combination_shape(type_: str, products: list[dict], recommended_ratio: list[dict] | None = None) -> None:
    expected_count = COMPONENT_COUNT_BY_TYPE.get(type_)
    if not expected_count:
        raise ValueError(f'Unknown combination type "{type_}" — must be HYBRID, TRIBRID, or QUADBRID.')
    products = products or []
    unique_titles = {normalize_product_name(p["title"]) for p in products}
    if len(unique_titles) != expected_count or len(products) != expected_count:
        raise ValueError(
            f"A {type_} must contain exactly {expected_count} distinct source products "
            f"(got {len(products)}, {len(unique_titles)} distinct)."
        )
    if recommended_ratio is not None:
        if any(r["ratioPercent"] >= 100 for r in recommended_ratio):
            raise ValueError("No single product may be assigned 100% of a custom combination.")
        pct_sum = sum(r["ratioPercent"] for r in recommended_ratio)
        if pct_sum != 100:
            raise ValueError(f"Ratios must sum to 100% (got {pct_sum}%).")


ROLE_PARTS = {
    "Freshness": 2,
    "Main fruit body": 2,
    "Floral bridge": 2,
    "Sweetness": 1,
    "Musk/wood base": 1,
    "Longevity support": 1,
    "Contrast": 1,
}
SWEET_HEAVY_MAX_PERCENT = 35
SWEET_HEAVY_ROLES = {"Sweetness", "Musk/wood base", "Longevity support"}
HEAVY_ROLES = {"Musk/wood base", "Longevity support"}
LIGHT_ROLES = {"Freshness", "Main fruit body"}


def compute_ratios(roled_products: list[dict]) -> list[dict]:
    is_two_balanced_fruity = len(roled_products) == 2 and all(p["role"] == "Main fruit body" for p in roled_products)
    parts = [1 for _ in roled_products] if is_two_balanced_fruity else [ROLE_PARTS.get(p["role"], 1) for p in roled_products]

    total_parts = sum(parts)
    percentages = [(p / total_parts) * 100 for p in parts]

    for i, p in enumerate(roled_products):
        if p["role"] in SWEET_HEAVY_ROLES and percentages[i] > SWEET_HEAVY_MAX_PERCENT:
            excess = percentages[i] - SWEET_HEAVY_MAX_PERCENT
            percentages[i] = SWEET_HEAVY_MAX_PERCENT
            others_total = sum(pct for j, pct in enumerate(percentages) if j != i)
            if others_total > 0:
                for j in range(len(percentages)):
                    if j != i:
                        percentages[j] += (percentages[j] / others_total) * excess

    rounded_percent = [round(p) for p in percentages]
    percent_diff = 100 - sum(rounded_percent)
    if percent_diff != 0:
        idx = rounded_percent.index(max(rounded_percent))
        rounded_percent[idx] += percent_diff

    raw_ml = [(pct / 100) * BOTTLE_ML for pct in rounded_percent]
    rounded_ml = [round(v * 10) / 10 for v in raw_ml]
    ml_diff = round((BOTTLE_ML - sum(rounded_ml)) * 10) / 10
    if ml_diff != 0:
        idx = rounded_ml.index(max(rounded_ml))
        rounded_ml[idx] = round((rounded_ml[idx] + ml_diff) * 10) / 10

    return [
        {"productTitle": p["title"], "parts": parts[i], "ratioPercent": rounded_percent[i], "milliliters": rounded_ml[i]}
        for i, p in enumerate(roled_products)
    ]


def _compute_customer_facing_strength(roled_products: list[dict], recommended_ratio: list[dict]) -> str:
    ratio_by_title = {r["productTitle"]: r["ratioPercent"] for r in recommended_ratio}
    heavy_share = 0
    light_share = 0
    for p in roled_products:
        pct = ratio_by_title.get(p["title"], 0)
        if p["role"] in HEAVY_ROLES:
            heavy_share += pct
        elif p["role"] in LIGHT_ROLES:
            light_share += pct
    if heavy_share >= 30:
        return "strong"
    if heavy_share == 0 and light_share >= 50:
        return "light"
    return "moderate"


def compute_evidence_scope(anchor: dict, profile: dict | None) -> str:
    location_verified = bool((profile or {}).get("locationVerified"))
    if location_verified and anchor.get("sameCityOrders", 0) > 0:
        return "city"
    if location_verified and anchor.get("sameStateOrders", 0) > 0:
        return "state"
    if location_verified and anchor.get("sameCountryOrders", 0) > 0:
        return "country"
    if anchor.get("sameSeasonOrders", 0) > 0:
        return "season_global"
    if anchor.get("distinctSimilarCustomers", 0) > 0 or anchor.get("repeatPurchaseCustomers", 0) > 0:
        return "global"
    return "limited"


_EVIDENCE_SCOPE_WEATHER_TEMPLATES = {
    "city": lambda season: f"This direction has performed well among customers in your region during {season or 'this'} season.",
    "state": lambda season: f"This direction has performed well among customers in your region during {season or 'this'} season.",
    "country": lambda season: f"This direction has performed well among customers in your region during {season or 'this'} season.",
    "season_global": lambda season: f"This direction has shown wider interest during similar {season or ''} seasonal conditions.".replace("  ", " "),
    "global": lambda _season: "This direction has shown broader interest among customers with similar preferences.",
    "limited": lambda _season: "Historical evidence is limited, so this recommendation relies more heavily on compatibility and your stated preferences.",
}


def _describe_weather_suitability(evidence_scope: str, profile: dict | None) -> str:
    profile = profile or {}
    season = profile.get("season")
    template = _EVIDENCE_SCOPE_WEATHER_TEMPLATES.get(evidence_scope, _EVIDENCE_SCOPE_WEATHER_TEMPLATES["limited"])
    base = template(season)
    had_conflict = has_season_weather_conflict(profile.get("requestedSeasonStyle"), profile.get("weatherDirection"))
    current_weather = profile.get("currentWeather") or {}
    if not profile.get("requestedSeasonStyle") and current_weather.get("condition"):
        return f"{base} Shaped around today's real conditions ({current_weather['condition']})."
    if had_conflict and profile.get("seasonStyleConflictResolved") and profile.get("requestedSeasonStyle"):
        return f"{base} Built around the classic {profile['requestedSeasonStyle']} character you asked for, even on an unusually different-feeling day."
    return base


def _describe_customer_facing_risk(risks: list[str]) -> str | None:
    if not risks:
        return None
    risk = risks[0]
    if "summer heat" in risk:
        return "This blend leans rich and could feel heavy in warm weather."
    if "heavy oud/leather/smoke/tobacco/resin" in risk:
        return "This blend is bold and long-lasting — it may feel strong for light, everyday wear."
    if "fruity products may compete" in risk:
        return "A few bright, fruity impressions are layered together, so the character may shift as it wears."
    if "spicy products may clash" in risk:
        return "This blend carries noticeable spice, which may read as bolder than a subtle everyday scent."
    if "citrus alongside dense smoky" in risk:
        return "This blend pairs a bright opening with a deeper base, which can feel like two phases as it wears."
    if "Four products in one blend" in risk:
        return "This is a more complex, layered blend, with a small risk of feeling less unified than a simpler one."
    if "no contrasting role" in risk:
        return "Every part of this blend leans the same direction, so it may feel one-note rather than layered."
    return "This recommendation carries a minor fit consideration worth knowing about."


NAME_NOUNS = ["Edition", "Signature", "Reserve", "Essence", "Element", "Momentum", "Aura", "Motion", "Story", "Statement"]


def _generate_customer_facing_name(primary_direction: str, seed: str) -> str:
    word = pick_words(DIRECTION_VOCABULARY[primary_direction], 1, seed + "name")[0]
    hash_value = _js_hash(seed)
    noun = NAME_NOUNS[hash_value % len(NAME_NOUNS)]
    return f"{word[0].upper()}{word[1:]} {noun}"


def _describe_best_use(profile: dict | None, primary_direction: str) -> str:
    occasion = ((profile or {}).get("occasion") or "").strip()
    by_direction = {
        "light_energetic": "daytime wear, active days, and warmer settings",
        "smooth_professional": "daily work, daytime and indoor professional settings",
        "sweet_comforting": "casual, relaxed occasions and cooler weather",
        "deep_evening": "evenings, formal occasions, and cooler weather",
    }
    setting_text = by_direction.get(primary_direction, by_direction["smooth_professional"])
    return f"Great for {occasion}, and works well for {setting_text}." if occasion else f"Works well for {setting_text}."


_BUNDLE_KEYWORD_PATTERN = re.compile(r"\b(bundle|gift set|giftset|duo pack|trio pack|value pack|set of \d)\b", re.IGNORECASE)
_FINISHED_COMBINATION_COLLECTIONS = {"hybrid", "tribrid", "quadbrid"}


def _is_eligible_combination_component(product: dict, finished_combination_titles: set[str]) -> bool:
    if product["normalizedTitle"] in finished_combination_titles:
        return False
    if _BUNDLE_KEYWORD_PATTERN.search(product["title"]):
        return False
    collection = product.get("collection")
    if collection and str(collection).lower() in _FINISHED_COMBINATION_COLLECTIONS:
        return False
    return True


def _build_support_shortlist_for_anchor(
    anchor: dict, all_products: list[dict], finished_combination_titles: set[str],
    preference_intent: dict | None, hard_exclude_families: list[str] | None = None, hard_exclude_terms: list[str] | None = None,
) -> list[dict]:
    hard_exclude_families = hard_exclude_families or []
    hard_exclude_terms = hard_exclude_terms or []
    anchor_families = _families_of(anchor.get("orderHistoryNotes"))
    anchor_notes = {str(n).lower() for n in (anchor.get("orderHistoryNotes") or [])}

    scored = []
    for product in all_products:
        if product["normalizedTitle"] == anchor.get("normalizedProductName"):
            continue
        if not _is_eligible_combination_component(product, finished_combination_titles):
            continue
        if not passes_intensity_filter(product["notesJson"], preference_intent):
            continue
        if _is_hard_excluded(product["notesJson"], hard_exclude_families, hard_exclude_terms):
            continue
        families = _families_of(product["notesJson"])
        compatible_count = sum(1 for af in anchor_families if any(pair_is_compatible(af, f) for f in families))
        if compatible_count == 0:
            continue
        if _note_overlap_ratio(anchor.get("orderHistoryNotes"), product["notesJson"]) > NEAR_DUPLICATE_OVERLAP_RATIO:
            continue
        note_overlap = sum(1 for n in (product["notesJson"] or []) if str(n).lower() in anchor_notes)
        scored.append({"product": product, "compatibleCount": compatible_count, "noteOverlap": note_overlap, "score": compatible_count * 5 + note_overlap})
    scored.sort(key=lambda s: -s["score"])
    return scored[:MAX_SUPPORT_SHORTLIST]


def _find_analogous_combinations(combo_products: list[dict], all_combinations: list[dict], notes_by_normalized_title: dict[str, list[str]]) -> list[dict]:
    combo_notes = {str(n).lower() for p in combo_products for n in (p.get("notes") or [])}
    analogous = []
    for combo in all_combinations:
        component_notes = set()
        for name in (combo["componentProductsJson"] if isinstance(combo["componentProductsJson"], list) else []):
            for n in notes_by_normalized_title.get(normalize_product_name(name), []):
                component_notes.add(str(n).lower())
        overlap = component_notes & combo_notes
        if len(overlap) >= 2:
            analogous.append({"title": combo["title"], "type": combo["type"], "tagLine": combo["tagLine"], "overlapCount": len(overlap)})
    analogous.sort(key=lambda a: -a["overlapCount"])
    return analogous[:3]


MAX_HISTORY_SCORE = 6
# Verified live (a real conversation, and reproduced locally): a customer who states exactly one
# real preference family and nothing else -- "fruity", matching real, correctly-detected product
# notes -- structurally cannot reach 3. matchesLike only floors at 5*0.2=1.0 per matching
# component (real DUA notes lists run 10-20 notes deep, so match strength for one family is
# almost always near that floor), family_breadth_coverage_score contributes nothing below two
# distinct families, and exact_note_coverage_score contributes nothing without a literal note
# name. Measured customerFitScore across 8 real candidates for this exact profile: 1.0-2.67 --
# every one landed "low" and got hard-blocked, even though the family match itself was completely
# correct. 2 is the lowest threshold that still filters out the genuinely weak case (a single
# component barely matching) while accepting the normal case (both components in a match,
# scoring 2.0) -- customer_fit_low remains an absolute block in evaluate_auto_confirm_eligibility,
# so this is the one number that actually controls whether a single stated preference is usable.
CUSTOMER_FIT_LOW_THRESHOLD = 2


def compute_history_score(anchor: dict) -> float:
    raw_history_score = (
        (SCORE_WEIGHTS["sameCity"] if anchor.get("sameCityOrders", 0) > 0 else 0)
        + (SCORE_WEIGHTS["sameCountry"] if anchor.get("sameCountryOrders", 0) > 0 else 0)
        + (SCORE_WEIGHTS["sameStateRegionOrClimate"] if anchor.get("sameStateOrders", 0) > 0 else 0)
        + (SCORE_WEIGHTS["repeatPurchaseBySimilarCustomer"] if anchor.get("repeatPurchaseCustomers", 0) > 0 else 0)
        + (SCORE_WEIGHTS["popularAmongSimilarCustomers"] if anchor.get("distinctSimilarCustomers", 0) >= 5 else 0)
    )
    return min(raw_history_score, MAX_HISTORY_SCORE)


def _score_proposed_combination(
    *, combo_products: list[dict], type_: str, component_key: str, profile: dict | None, anchor: dict,
    all_combinations: list[dict], notes_by_normalized_title: dict[str, list[str]], vocab_used_words: set[str],
    preference_intent: dict | None, lifestyle_context: dict | None,
) -> dict | None:
    for i in range(len(combo_products)):
        for j in range(i + 1, len(combo_products)):
            if _note_overlap_ratio(combo_products[i].get("notes"), combo_products[j].get("notes")) > NEAR_DUPLICATE_OVERLAP_RATIO:
                return None
    unique_title_count = len({normalize_product_name(p["title"]) for p in combo_products})
    if unique_title_count != len(combo_products):
        return None

    profile = profile or {}
    season = profile.get("season")
    likes = profile.get("likes") or []
    dislikes = profile.get("dislikes") or []
    like_families = text_to_preference_families(likes)
    literal_like_terms = literal_note_terms_from_likes(likes)
    split = split_dislikes_by_exactness(dislikes)
    exact_note_dislikes = split["exactNoteDislikes"]
    dislike_families = split["explicitFamilyDislikes"]

    conflict_penalty = 0
    for p in combo_products:
        if has_hard_excluded_term(p.get("notes"), exact_note_dislikes):
            return None
        conflict = classify_dislike_conflict(p.get("notes"), dislike_families)
        if conflict["severity"] == "high":
            return None
        if conflict["severity"] == "medium":
            conflict_penalty -= 5
        if conflict["severity"] == "low":
            conflict_penalty -= 2

    roled_products = assign_roles(combo_products)
    if sum(1 for p in roled_products if not p["hasDetectedFamily"]) >= 2:
        return None

    risk_context = {"season": season, "likeFamilies": like_families, "strengthPreference": (preference_intent or {}).get("strengthPreference"), "roledProducts": roled_products}
    risks = assess_combination_risks(
        [{"title": p["title"], "notes": p.get("notes")} for p in combo_products], risk_context
    )
    risk_details = assess_combination_risk_details(
        [{"title": p["title"], "notes": p.get("notes")} for p in combo_products], risk_context
    )
    if risk_details["hasCritical"]:
        return None

    preference_score = 0.0
    matched_preference_families: list[str] = []
    seen_families = set()
    for p in combo_products:
        matches = matched_likes(p.get("notes"), like_families)
        for family in matches:
            if family not in seen_families:
                seen_families.add(family)
                matched_preference_families.append(family)
            preference_score += SCORE_WEIGHTS["matchesLike"] * max(0.2, like_match_strength(p.get("notes"), family))
    preference_score += family_breadth_coverage_score(len(matched_preference_families))
    requested_preference_families = like_families
    missing_preference_families = [f for f in like_families if f not in matched_preference_families]
    floral_role_strength = max([0.0] + [p.get("floralCoverage", 0) for p in roled_products])

    combo_all_notes = [n for p in combo_products for n in (p.get("notes") or [])]
    total_literal_matches = literal_note_match_count(combo_all_notes, literal_like_terms)
    exact_note_score = exact_note_coverage_score(total_literal_matches)
    preference_score += exact_note_score

    if len(like_families) > 0 and len(matched_preference_families) == 0:
        return None
    if len(literal_like_terms) > 0 and total_literal_matches == 0:
        return None

    seasonal_score = 0 if any("summer heat" in r for r in risks) else SCORE_WEIGHTS["sameSeason"]
    history_score = compute_history_score(anchor)

    compatibility_score = 0
    compatibility_reasons = []
    for i in range(len(combo_products)):
        for j in range(i + 1, len(combo_products)):
            fam_a = _families_of(combo_products[i].get("notes"))
            fam_b = _families_of(combo_products[j].get("notes"))
            match = next(((a, b) for a in fam_a for b in fam_b if pair_is_compatible(a, b)), None)
            if match:
                compatibility_score += 5
                compatibility_reasons.append(f"{combo_products[i]['title']}'s {match[0]} pairs well with {combo_products[j]['title']}'s {match[1]}")

    analogous_existing_combinations = _find_analogous_combinations(combo_products, all_combinations, notes_by_normalized_title)
    analogous_score = len(analogous_existing_combinations) * 2

    has_excessive_direction_stacking = any(r["id"] == "excessive_direction_stacking" for r in risk_details["breakdown"])
    balance_risk_hit = has_excessive_direction_stacking or any(re.search(r"compete|duplicate|complex|no contrasting role", r, re.IGNORECASE) for r in risks)
    balance_score = 0 if balance_risk_hit else 10
    roles_complementary = (not balance_risk_hit) and all(p["hasDetectedFamily"] for p in roled_products)

    preferred_directions = (preference_intent or {}).get("preferredDirections") or []
    avoided_directions = (preference_intent or {}).get("avoidedDirections") or []
    style_match_score = 0
    for direction in preferred_directions:
        if count_preferred_direction_matches(combo_all_notes, [direction]) > 0:
            style_match_score += 3
    avoided_direction_penalty = 0
    for direction in avoided_directions:
        if count_avoided_direction_matches(combo_all_notes, [direction]) > 0:
            avoided_direction_penalty -= 5

    lifestyle_match_score = 0.0
    lifestyle_preferred = (lifestyle_context or {}).get("preferredDirections") or {}
    lifestyle_avoided = (lifestyle_context or {}).get("avoidedDirections") or {}
    for direction, weight in lifestyle_preferred.items():
        if count_preferred_direction_matches(combo_all_notes, [direction]) > 0:
            lifestyle_match_score += 3 * weight
    lifestyle_conflict_penalty = 0.0
    for direction, weight in lifestyle_avoided.items():
        if count_avoided_direction_matches(combo_all_notes, [direction]) > 0:
            lifestyle_conflict_penalty -= 5 * weight

    powdery_product_count = sum(1 for p in combo_products if "powdery" in detect_families(p.get("notes"), PREFERENCE_FAMILIES))
    powdery_context_penalty = 0
    if powdery_product_count >= 2 and "powdery" not in like_families:
        sensitive_or_light = (
            (preference_intent or {}).get("sensitivityLevel") == "high"
            or any(d in ("relaxing", "airy", "clean", "watery", "light-fruity") for d in preferred_directions)
            or any(d in ("airy", "crisp", "watery", "citrus-forward") for d in lifestyle_preferred.keys())
        )
        if sensitive_or_light:
            powdery_context_penalty = -8

    combined_unique_note_count = len({str(n).lower() for p in combo_products for n in (p.get("notes") or [])})
    combined_complexity = compute_complexity_level(combined_unique_note_count)
    prefer_simple = bool((preference_intent or {}).get("preferSimpleCombinations"))
    complexity_penalty_table = {
        "low": 0,
        "moderate": -2 if prefer_simple else -1,
        "high": -6 if prefer_simple else -2,
        "very-high": -12 if prefer_simple else -4,
    }
    complexity_penalty = complexity_penalty_table.get(combined_complexity, 0)

    type_simplicity_score_table = (
        {"HYBRID": 14, "TRIBRID": -10, "QUADBRID": -22} if prefer_simple
        else {"HYBRID": 10, "TRIBRID": -6, "QUADBRID": -16}
    )
    type_simplicity_score = type_simplicity_score_table.get(type_, 0)

    final_score = (
        preference_score + seasonal_score + history_score + compatibility_score + analogous_score + balance_score + conflict_penalty
        + style_match_score + avoided_direction_penalty + complexity_penalty + type_simplicity_score
        + lifestyle_match_score + lifestyle_conflict_penalty + powdery_context_penalty + risk_details["riskPenalty"]
    )

    evidence_scope = compute_evidence_scope(anchor, profile)
    # Phase 7B: this used to hard-require a verified location -- meaning no conversation could
    # ever reach "high" customerFit confidence (and therefore autoConfirmEligible) without one,
    # regardless of how much other signal existed. That directly undercut Phase 7's own
    # confidence-based readiness policy: a customer could give occasion + style + hard dislikes
    # (everything is_profile_ready_for_analysis considers "enough to generate") and still get
    # stuck at customer_fit_confidence="low" forever. Reusing the same readiness check here keeps
    # the two policies in sync by construction instead of drifting apart.
    profile_complete = is_profile_ready_for_analysis(profile)
    season_unresolved = has_season_weather_conflict(profile.get("requestedSeasonStyle"), profile.get("weatherDirection")) and profile.get("seasonStyleConflictResolved") is False

    if final_score >= 30 and len(risks) == 0:
        confidence = "very high"
    elif final_score >= 20 and len(risks) <= 1:
        confidence = "high"
    elif final_score >= 10 and len(risks) <= 2:
        confidence = "medium"
    else:
        confidence = "low"

    confidence_rank = {"low": 0, "medium": 1, "high": 2, "very high": 3}

    def cap(label: str) -> None:
        nonlocal confidence
        if confidence_rank[confidence] > confidence_rank[label]:
            confidence = label

    if history_score == 0:
        cap("medium")
    if not profile.get("locationVerified"):
        cap("medium")
    if not roles_complementary:
        cap("medium")
    if season_unresolved:
        cap("medium")
    if len(risks) > 0:
        cap("high")
    if not profile_complete:
        cap("high")
    if evidence_scope in ("limited", "global"):
        cap("medium")
    if risk_details["riskPenalty"] <= -10:
        cap("low")
    if prefer_simple and combined_complexity == "very-high":
        cap("low")
    elif prefer_simple and combined_complexity == "high":
        cap("medium")

    historical_confidence_by_scope = {"city": "high", "state": "high", "country": "medium", "season_global": "medium", "global": "low", "limited": "low"}
    historical_confidence = historical_confidence_by_scope.get(evidence_scope, "low")

    min_note_count = min(len(p.get("notes") or []) for p in combo_products)
    data_confidence = "high" if min_note_count >= 3 else ("medium" if min_note_count >= 1 else "low")

    compatibility_confidence = (
        "high" if risk_details["riskPenalty"] == 0 and roles_complementary
        else ("medium" if risk_details["riskPenalty"] > -10 else "low")
    )
    novelty_confidence = "high" if len(analogous_existing_combinations) >= 2 else ("medium" if len(analogous_existing_combinations) >= 1 else "low")

    customer_fit_raw = preference_score + style_match_score + lifestyle_match_score
    customer_fit_confidence = (
        "low" if not profile_complete
        else ("high" if customer_fit_raw >= 8 else ("medium" if customer_fit_raw >= CUSTOMER_FIT_LOW_THRESHOLD else "low"))
    )

    confidence_breakdown = {
        "data": {"value": data_confidence, "reason": "Every component has a full, real note list on file." if data_confidence == "high" else "At least one component's real note list is thin."},
        "historical": {"value": historical_confidence, "reason": f"Evidence scope: {evidence_scope}."},
        "compatibility": {"value": compatibility_confidence, "reason": f"{len(risks)} real compatibility risk(s) identified." if risks else "No compatibility risks identified; roles are complementary."},
        "novelty": {"value": novelty_confidence, "reason": f"{len(analogous_existing_combinations)} analogous existing combination(s) share real notes with this one." if analogous_existing_combinations else "No closely analogous existing combination found."},
        "customerFit": {"value": customer_fit_confidence, "reason": "Profile is missing required signal (location/likes/style)." if not profile_complete else f"Preference/style/lifestyle match score: {customer_fit_raw}."},
    }
    if data_confidence == "low":
        cap("low")
    if customer_fit_confidence == "low":
        cap("medium")

    recommended_ratio = compute_ratios(roled_products)

    primary_direction = direction_for_role(roled_products[0].get("role", "Contrast") if roled_products else "Contrast")
    customer_facing_description = describe_character([p["role"] for p in roled_products], component_key, vocab_used_words)
    customer_facing_best_use = _describe_best_use(profile, primary_direction)
    customer_facing_weather_suitability = _describe_weather_suitability(evidence_scope, profile)
    customer_facing_strength = _compute_customer_facing_strength(roled_products, recommended_ratio)
    customer_facing_risk = _describe_customer_facing_risk(risks)

    total_intensity_drivers = sum(len(p.get("intensityDrivers") or []) for p in roled_products)
    is_overall_intense = total_intensity_drivers >= 3 or combined_complexity == "very-high" or customer_facing_strength == "strong"
    naming_direction = "deep_evening" if is_overall_intense else primary_direction
    customer_facing_name = _generate_customer_facing_name(naming_direction, component_key)
    customer_facing_why_suits = (
        f"Designed around your preference for {' and '.join(matched_preference_families)} scents."
        if matched_preference_families else "Designed to be a versatile, easy-to-wear everyday option."
    )

    internal_products = [{"title": p["title"], "notes": p.get("notes"), "fragranceFamily": None, "contribution": p["role"]} for p in roled_products]

    customer_facing_notes_by_product = [{"label": p["title"], "notes": (p["notes"] or [])[:5]} for p in internal_products]
    ratio_by_title = {r["productTitle"]: r["ratioPercent"] for r in recommended_ratio}
    components = [
        {
            "productName": p["title"],
            "availableNotes": (p["notes"] or [])[:5],
            "contribution": p["contribution"],
            "ratioPercent": ratio_by_title.get(p["title"]),
        }
        for p in internal_products
    ]
    if len(internal_products) < 2:
        shared_or_connecting_notes: list[str] = []
    else:
        note_sets = [{str(n).lower() for n in (p["notes"] or [])} for p in internal_products]
        shared_lower = {n for n in note_sets[0] if all(n in s for s in note_sets[1:])}
        shared_or_connecting_notes = [n for n in (internal_products[0]["notes"] or []) if str(n).lower() in shared_lower]

    why_notes_work = (
        " ".join(compatibility_reasons[:3]) if compatibility_reasons
        else "Each component plays a distinct, real role in the blend rather than repeating the same direction."
    )
    expected_result = f"A {customer_facing_description} result, built from {' and '.join(p['title'] for p in internal_products)}."

    validate_combination_shape(type_, internal_products, recommended_ratio)

    return {
        "internalProducts": internal_products,
        "canonicalKey": component_key,
        "compatibilityReasons": compatibility_reasons,
        "historicalEvidence": {
            "sameCityOrders": anchor.get("sameCityOrders", 0),
            "sameStateOrders": anchor.get("sameStateOrders", 0),
            "sameCountryOrders": anchor.get("sameCountryOrders", 0),
            "sameSeasonOrders": anchor.get("sameSeasonOrders", 0),
            "distinctSimilarCustomers": anchor.get("distinctSimilarCustomers", 0),
            "repeatPurchaseCustomers": anchor.get("repeatPurchaseCustomers", 0),
        },
        "customerFacingHistoricalEvidence": {
            "cityEvidence": f"{anchor.get('sameCityOrders', 0)} historical order(s) from the same city." if anchor.get("sameCityOrders", 0) > 0 else "No same-city order history available.",
            "countryEvidence": f"{anchor.get('sameCountryOrders', 0)} historical order(s) from the same country." if anchor.get("sameCountryOrders", 0) > 0 else "No same-country order history available.",
            "seasonalEvidence": f"{anchor.get('sameSeasonOrders', 0)} historical order(s) during the same season." if anchor.get("sameSeasonOrders", 0) > 0 else "No same-season order history available.",
            "repeatEvidence": f"{anchor.get('repeatPurchaseCustomers', 0)} similar customer(s) repeat-purchased this direction." if anchor.get("repeatPurchaseCustomers", 0) > 0 else "No repeat-purchase evidence available.",
            "dataWindow": "Reflects real historical order data through October 2024 — not a claim of current popularity.",
        },
        "existingCombinationEvidence": {"exactCombinationExists": False, "similarEvidence": analogous_existing_combinations},
        "components": components,
        "combinedDirection": customer_facing_description,
        "sharedOrConnectingNotes": shared_or_connecting_notes,
        "whyNotesWork": why_notes_work,
        "expectedResult": expected_result,
        "analogousExistingCombinations": analogous_existing_combinations,
        "preferenceScore": preference_score,
        "seasonalScore": seasonal_score,
        "historyScore": history_score,
        "compatibilityScore": compatibility_score,
        "customerFitScore": customer_fit_raw,
        "balanceScore": balance_score,
        "conflictPenalty": conflict_penalty,
        "styleMatchScore": style_match_score,
        "avoidedDirectionPenalty": avoided_direction_penalty,
        "complexityPenalty": complexity_penalty,
        "typeSimplicityScore": type_simplicity_score,
        "lifestyleMatchScore": lifestyle_match_score,
        "lifestyleConflictPenalty": lifestyle_conflict_penalty,
        "powderyContextPenalty": powdery_context_penalty,
        "matchedLifestyles": (lifestyle_context or {}).get("lifestyles") or [],
        "combinedComplexity": combined_complexity,
        "finalScore": final_score,
        "recommendedRatio": recommended_ratio,
        "risks": risks,
        "riskPenalty": risk_details["riskPenalty"],
        "riskBreakdown": risk_details["breakdown"],
        "exactNoteCoverageScore": exact_note_score,
        "requestedPreferenceFamilies": requested_preference_families,
        "matchedPreferenceFamilies": matched_preference_families,
        "missingPreferenceFamilies": missing_preference_families,
        "floralRoleStrength": floral_role_strength,
        "type": type_,
        "existsAlready": False,
        "evidenceScope": evidence_scope,
        "confidence": confidence,
        "confidenceBreakdown": confidence_breakdown,
        "customerFacingName": customer_facing_name,
        "customerFacingDescription": customer_facing_description,
        "customerFacingWhySuits": customer_facing_why_suits,
        "customerFacingBestUse": customer_facing_best_use,
        "customerFacingWeatherSuitability": customer_facing_weather_suitability,
        "customerFacingStrength": customer_facing_strength,
        "customerFacingRisk": customer_facing_risk,
        "customerFacingNotesByProduct": customer_facing_notes_by_product,
    }


def _generate_combos_for_anchor(anchor: dict, ctx: dict) -> list[dict]:
    proposals = []
    shortlist = _build_support_shortlist_for_anchor(
        anchor, ctx["allProducts"], ctx["finishedCombinationTitles"], ctx["preferenceIntent"],
        ctx["hardExcludeFamilies"], ctx["hardExcludeTerms"],
    )
    if not shortlist:
        return proposals

    for type_ in ctx["allowedTypes"]:
        support_count = COMPONENT_COUNT_BY_TYPE.get(type_, 0) - 1
        if support_count < 1 or support_count > len(shortlist):
            continue

        for support_combo in combinations(shortlist, support_count):
            combo_products = [
                {"title": anchor["productName"], "notes": anchor.get("orderHistoryNotes")},
                *[{"title": s["product"]["title"], "notes": s["product"]["notesJson"] or []} for s in support_combo],
            ]
            component_key = create_combination_key([p["title"] for p in combo_products])
            if component_key in ctx["seenComponentKeys"]:
                continue
            ctx["seenComponentKeys"].add(component_key)

            if component_key in ctx["existingComponentKeys"]:
                continue

            proposal = _score_proposed_combination(
                combo_products=combo_products, type_=type_, component_key=component_key, profile=ctx["profile"],
                anchor=anchor, all_combinations=ctx["allCombinations"], notes_by_normalized_title=ctx["notesByNormalizedTitle"],
                vocab_used_words=ctx["vocabUsedWords"], preference_intent=ctx["preferenceIntent"], lifestyle_context=ctx["lifestyleContext"],
            )
            if proposal:
                proposals.append(proposal)
    return proposals


FALLBACK_ANCHORS_PER_MISSING_TERM = 3


def build_fallback_anchors_for_missing_terms(
    missing_terms: list[str], *, all_products: list[dict], finished_combination_titles: set[str],
    preference_intent: dict | None, candidate_products: list[dict] | None,
    hard_exclude_families: list[str] | None = None, hard_exclude_terms: list[str] | None = None,
) -> list[dict]:
    hard_exclude_families = hard_exclude_families or []
    hard_exclude_terms = hard_exclude_terms or []
    by_normalized_title = {c["normalizedProductName"]: c for c in (candidate_products or [])}
    seen = set()
    anchors = []
    for term in missing_terms:
        added = 0
        for product in all_products:
            if added >= FALLBACK_ANCHORS_PER_MISSING_TERM:
                break
            if product["normalizedTitle"] in seen:
                continue
            if not _is_eligible_combination_component(product, finished_combination_titles):
                continue
            if not passes_intensity_filter(product["notesJson"], preference_intent):
                continue
            if _is_hard_excluded(product["notesJson"], hard_exclude_families, hard_exclude_terms):
                continue
            if len(matched_literal_terms(product["notesJson"], [term])) == 0:
                continue

            seen.add(product["normalizedTitle"])
            added += 1
            existing = by_normalized_title.get(product["normalizedTitle"])
            anchors.append(existing or {
                "productName": product["title"],
                "normalizedProductName": product["normalizedTitle"],
                "collection": product.get("collection"),
                "orderHistoryNotes": product["notesJson"] or [],
                "sameCityOrders": 0, "sameStateOrders": 0, "sameCountryOrders": 0, "sameSeasonOrders": 0,
                "distinctSimilarCustomers": 0, "repeatPurchaseCustomers": 0,
            })
    return anchors


async def generate_new_product_combinations(
    session: AsyncSession, *, profile: dict | None, candidate_products: list[dict] | None,
    maximum_results: int = DEFAULT_MAX_RESULTS, allowed_types: list[str] | None = None,
    hard_exclude_families: list[str] | None = None, hard_exclude_terms: list[str] | None = None,
) -> list[dict]:
    allowed_types = allowed_types or ALL_TYPES
    hard_exclude_families = hard_exclude_families or []
    hard_exclude_terms = hard_exclude_terms or []
    profile = profile or {}
    candidate_products = candidate_products or []

    catalog = await _get_catalog_and_combinations(session)
    all_products, all_combinations = catalog["allProducts"], catalog["allCombinations"]
    notes_by_normalized_title = {p["normalizedTitle"]: p["notesJson"] or [] for p in all_products}
    existing_component_keys = {c["componentKey"] for c in all_combinations}
    finished_combination_titles = {c["normalizedTitle"] for c in all_combinations}
    preference_intent = interpret_customer_preferences(profile)
    lifestyle_context = interpret_lifestyle_context(profile)

    anchors = sorted(
        (
            c for c in candidate_products
            if _is_eligible_combination_component(
                {"title": c["productName"], "normalizedTitle": c["normalizedProductName"], "collection": c.get("collection")},
                finished_combination_titles,
            )
            and passes_intensity_filter(c.get("orderHistoryNotes"), preference_intent)
            and not _is_hard_excluded(c.get("orderHistoryNotes"), hard_exclude_families, hard_exclude_terms)
        ),
        key=lambda c: -c["relevanceScore"],
    )[:MAX_ANCHORS]
    if not anchors:
        return []

    results: list[dict] = []
    seen_component_keys: set[str] = set()
    vocab_used_words: set[str] = set()
    gen_ctx = {
        "allProducts": all_products, "finishedCombinationTitles": finished_combination_titles,
        "preferenceIntent": preference_intent, "allowedTypes": allowed_types, "profile": profile,
        "allCombinations": all_combinations, "notesByNormalizedTitle": notes_by_normalized_title,
        "lifestyleContext": lifestyle_context, "vocabUsedWords": vocab_used_words,
        "existingComponentKeys": existing_component_keys, "seenComponentKeys": seen_component_keys,
        "hardExcludeFamilies": hard_exclude_families, "hardExcludeTerms": hard_exclude_terms,
    }

    for anchor in anchors:
        results.extend(_generate_combos_for_anchor(anchor, gen_ctx))

    results.sort(key=lambda r: -r["finalScore"])
    final_results = _select_diverse_results(results, maximum_results)

    literal_like_terms = literal_note_terms_from_likes(profile.get("likes") or [])
    fallback_used = False
    fallback_target_notes: list[str] = []
    if literal_like_terms:
        batch_notes = [n for r in final_results for p in r["internalProducts"] for n in (p.get("notes") or [])]
        missing = missing_literal_terms(batch_notes, literal_like_terms)
        if missing:
            fallback_used = True
            fallback_target_notes = missing
            fallback_anchors = build_fallback_anchors_for_missing_terms(
                missing, all_products=all_products, finished_combination_titles=finished_combination_titles,
                preference_intent=preference_intent, candidate_products=candidate_products,
                hard_exclude_families=hard_exclude_families, hard_exclude_terms=hard_exclude_terms,
            )
            for anchor in fallback_anchors:
                results.extend(_generate_combos_for_anchor(anchor, gen_ctx))
            results.sort(key=lambda r: -r["finalScore"])
            final_results = _select_diverse_results(results, maximum_results)

    for r in final_results:
        notes = [n for p in r["internalProducts"] for n in (p.get("notes") or [])]
        r["requestedExactNotes"] = literal_like_terms
        r["matchedExactNotes"] = matched_literal_terms(notes, literal_like_terms)
        r["missingExactNotes"] = missing_literal_terms(notes, literal_like_terms)
        r["fallbackUsed"] = fallback_used
        r["fallbackTargetNotes"] = fallback_target_notes

    catalog_titles_lowercase = [p["title"].lower() for p in all_products if len(p["title"]) >= 4]
    copy_items = []
    for proposal in final_results:
        notes_by_role: dict[str, list[str]] = {}
        for p in proposal["internalProducts"]:
            existing = notes_by_role.get(p["contribution"], [])
            notes_by_role[p["contribution"]] = list(dict.fromkeys([*existing, *(p.get("notes") or [])]))
        copy_items.append({"proposal": proposal, "notesByRole": notes_by_role})

    await apply_customer_facing_copy(
        copy_items,
        {"likes": profile.get("likes"), "dislikes": profile.get("dislikes"), "preferredStyle": profile.get("preferredStyle"), "occasion": profile.get("occasion")},
        catalog_titles_lowercase,
    )

    return final_results
