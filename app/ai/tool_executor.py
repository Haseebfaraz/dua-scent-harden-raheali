"""Tool execution, split into two trust levels (Phase 3, finding F3).

  * execute_model_tool(...)     -- the ONLY dispatcher the conversational model can reach. It
                                   accepts the four model-callable tools in app/ai/tools.py,
                                   validates arguments against strict schemas (unknown keys
                                   rejected, bounded, enums), and returns customer-safe results.
                                   Private catalog / candidate / generation tools are refused by
                                   name here even if a model asks for them.
  * execute_fragrance_tool(...) -- the PRIVATE dispatcher (server-only). It still exposes every
                                   original handler (candidate analysis, catalog lookups,
                                   generation, refinement, legacy selection/confirmation) for the
                                   server-side pipeline and for tests. Its results contain
                                   internal data and are never placed in model context.

The recommendation walk (`run_generate` / `run_refine` / `_auto_select_and_confirm_best`) returns
a structured outcome: a status, control data for the browser (recommendation id, preview URL),
and a CustomerSafeRecommendation. The status text handed to the model is server-authored guidance
only; ids, scores, titles, and inventory internals stay in the outcome fields that the
orchestrator never serializes for the model.
"""

import hashlib
import json
import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.preview_url import build_preview_url
from app.ai.refinement import derive_refinement_adjustments
from app.ai.safe_views import CustomerSafeRecommendation, build_customer_safe_recommendation_from_candidate
from app.ai.tools import MODEL_CALLABLE_TOOL_NAMES, ModelToolArgumentError, validate_model_tool_arguments, validate_profile_field_value
from app.db.models import FragranceRecommendation
from app.db.time import utcnow
from app.fragrance.compatibility import (
    SEVERITY_RANK,
    literal_note_match_count,
    split_dislikes_by_exactness,
    text_to_preference_families,
)
from app.fragrance.normalization import correct_preference_vocabulary, correct_preference_vocabulary_list
from app.fragrance.selection_parser import parse_recommendation_selection
from app.fragrance.weather import (
    describe_weather_simple,
    derive_weather_direction,
    get_calendar_season,
    has_season_weather_conflict,
    weather_direction_to_query_season,
)
from app.services.build_capability import issue_build_token, preview_url_for_logging
from app.services.combination_analysis import (
    check_exact_combination_exists,
    find_combinations_using_similar_notes,
    find_existing_combinations_for_product,
)
from app.services.customer_profile import get_customer_profile, get_missing_required_fields, save_customer_profile_fields
from app.services.location_verification import fetch_current_weather, verify_city
from app.services.odoo_inventory import evaluate_candidate_inventory
from app.services.order_history import analyze_customer_product_candidates
from app.services.product_catalog import get_product_notes_and_combination_status
from app.services.recommendation_confirmation import confirm_recommendation, save_recommendation
from app.services.recommendation_engine import CUSTOMER_FIT_LOW_THRESHOLD, generate_new_product_combinations, validate_combination_shape

logger = logging.getLogger(__name__)

# ============================================================
# Helpers
# ============================================================

_NO_REAL_STYLE_PATTERN = re.compile(
    r"you should know|you decide|surprise me|not sure|no preference|i ?don'?t know|\bidk\b|whatever you (think|want|pick)",
    re.IGNORECASE,
)
_NO_REAL_VALUE_PATTERN = re.compile(r"^(na|n/a|none|nothing|no)$", re.IGNORECASE)
_IMPLAUSIBLE_NAME_PATTERN = re.compile(
    r"\b(day|today|feeling|doing|tired|busy|great|good|bad|fine|ok|okay|nothing|well|not|having|going|alright|"
    r"stressed|happy|sad|meh|hello|hi|hey|yo|sup|greetings|user|guest|customer|client|admin|test|testing|"
    r"anonymous|unknown|nobody|somebody)\b",
    re.IGNORECASE,
)
_VOCABULARY_CORRECTED_FIELDS = {"likes", "dislikes", "preferredStyle", "occasion", "additionalPreferences"}

# Outcome statuses of the private recommendation walk. Only these labels (plus the server-authored
# guidance below) are ever shown to the model.
STATUS_READY = "READY"
STATUS_IDENTITY_NEEDED = "IDENTITY_NEEDED"
STATUS_NEEDS_MORE_DETAIL = "NEEDS_MORE_DETAIL"
STATUS_TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"

STATUS_GUIDANCE = {
    STATUS_IDENTITY_NEEDED: "A fragrance is ready to be locked in, but the customer's name and email are not available yet. Do NOT claim this is a stock or availability problem. Ask naturally for what is missing (their name, or to make sure they are signed in so their email is on file).",
    STATUS_NEEDS_MORE_DETAIL: "Nothing felt like a confident enough match yet. Tell the customer honestly that you want to get this right and ask ONE more useful question about their taste, rather than presenting a weak guess.",
    STATUS_TEMPORARILY_UNAVAILABLE: "The fragrance couldn't be finalized just now. Tell the customer there was a temporary hiccup preparing it and offer to try again shortly. Do not invent a reason.",
}


def _is_implausible_name(text: str) -> bool:
    trimmed = str(text).strip()
    if not trimmed:
        return True
    if len(trimmed.split()) > 4:
        return True
    return bool(_IMPLAUSIBLE_NAME_PATTERN.search(trimmed))


def _infer_style_from_profile(profile: dict) -> str:
    parts = []
    like_families = text_to_preference_families(profile.get("likes") or [])
    if like_families:
        parts.append(" & ".join(like_families))
    if profile.get("additionalPreferences"):
        parts.append(", ".join(profile["additionalPreferences"]))
    if profile.get("requestedSeasonStyle"):
        parts.append(f"{profile['requestedSeasonStyle']}-appropriate")
    return ", ".join(parts) if parts else "Versatile, easy-to-wear"


def _effective_query_season(profile: dict) -> str:
    if profile.get("requestedSeasonStyle"):
        return profile["requestedSeasonStyle"]
    return weather_direction_to_query_season(profile.get("weatherDirection"), get_calendar_season(profile.get("country")))


def compute_profile_hash(profile: dict) -> str:
    relevant = {
        "city": profile.get("city"), "stateRegion": profile.get("stateRegion"), "country": profile.get("country"),
        "requestedSeasonStyle": profile.get("requestedSeasonStyle"), "weatherDirection": profile.get("weatherDirection"),
        "likes": profile.get("likes"), "dislikes": profile.get("dislikes"), "preferredStyle": profile.get("preferredStyle"),
        "inferredStyle": profile.get("inferredStyle"), "additionalPreferences": profile.get("additionalPreferences"),
        "strengthPreference": profile.get("strengthPreference"), "occasion": profile.get("occasion"),
    }
    return hashlib.sha256(json.dumps(relevant, sort_keys=False).encode()).hexdigest()


_compute_profile_hash = compute_profile_hash


def _log_preview_event(stage: str, *, conversation_id, recommendation_id, preview_id, event_type, preview_url) -> None:
    # The preview URL carries the build capability token -- never logged in plaintext.
    logger.info("%s %s", stage, json.dumps({
        "conversationId": conversation_id, "recommendationId": recommendation_id,
        "previewId": preview_id, "eventType": event_type, "previewUrl": preview_url_for_logging(preview_url),
    }))


# Ephemeral, regenerable per-conversation scratch space -- NOT the durable CustomerProfileState.
# Process-global (lost on restart), exactly like the JS module-level Map.
_conversation_scratch: dict[str, dict[str, Any]] = {}


def _get_scratch(conversation_id: str, profile: dict | None = None) -> dict:
    if conversation_id not in _conversation_scratch:
        _conversation_scratch[conversation_id] = {"candidateProducts": None, "lastCombinations": None, "profileHash": None, "lastGenerationAttemptHash": None}
    scratch = _conversation_scratch[conversation_id]
    if profile:
        h = compute_profile_hash(profile)
        if scratch["profileHash"] and scratch["profileHash"] != h:
            scratch["candidateProducts"] = None
            scratch["lastCombinations"] = None
        scratch["profileHash"] = h
    return scratch


def get_scratch_for_testing(conversation_id: str) -> dict | None:
    return _conversation_scratch.get(conversation_id)


def mark_generation_attempted(conversation_id: str, profile: dict) -> None:
    _get_scratch(conversation_id)["lastGenerationAttemptHash"] = compute_profile_hash(profile)


def generation_already_attempted(conversation_id: str, profile: dict) -> bool:
    scratch = _conversation_scratch.get(conversation_id) or {}
    return scratch.get("lastGenerationAttemptHash") == compute_profile_hash(profile)


# ============================================================
# Auto-confirm eligibility gate (deterministic, no network call)
# ============================================================

def evaluate_auto_confirm_eligibility(candidate: dict, profile: dict | None) -> dict:
    profile = profile or {}
    breakdown = candidate.get("confidenceBreakdown") or {}
    customer_fit_confidence = (breakdown.get("customerFit") or {}).get("value")
    compatibility_confidence = (breakdown.get("compatibility") or {}).get("value")

    counted_risks = [r for r in (candidate.get("riskBreakdown") or []) if r.get("counted")]
    highest_counted_risk_severity = None
    for r in counted_risks:
        if highest_counted_risk_severity is None or SEVERITY_RANK[r["severity"]] > SEVERITY_RANK[highest_counted_risk_severity]:
            highest_counted_risk_severity = r["severity"]
    has_high_severity_risk = highest_counted_risk_severity in ("high", "critical")

    split = split_dislikes_by_exactness(profile.get("dislikes") or [])
    all_notes = [n for p in (candidate.get("internalProducts") or []) for n in (p.get("notes") or [])]
    has_hard_dislike_conflict = literal_note_match_count(all_notes, split["exactNoteDislikes"]) > 0

    shape_valid = True
    try:
        validate_combination_shape(candidate.get("type"), candidate.get("internalProducts"), candidate.get("recommendedRatio"))
    except Exception:
        shape_valid = False

    requested_preference_families = candidate.get("requestedPreferenceFamilies") or []
    matched_preference_families = candidate.get("matchedPreferenceFamilies") or []
    has_no_stated_preference_coverage = len(requested_preference_families) > 0 and len(matched_preference_families) == 0

    reasons = []
    if has_hard_dislike_conflict:
        reasons.append("hard_dislike_conflict")
    if not shape_valid:
        reasons.append("invalid_shape")
    if has_no_stated_preference_coverage:
        reasons.append("no_stated_preference_coverage")
    if has_high_severity_risk:
        reasons.append(f"high_severity_risk:{highest_counted_risk_severity}")
    if customer_fit_confidence == "low":
        reasons.append("customer_fit_low")
    if compatibility_confidence == "low":
        reasons.append("compatibility_low")

    return {
        "autoConfirmEligible": len(reasons) == 0,
        "autoConfirmReasons": reasons,
        "customerFitThreshold": CUSTOMER_FIT_LOW_THRESHOLD,
        "highestCountedRiskSeverity": highest_counted_risk_severity,
        "hasHardDislikeConflict": has_hard_dislike_conflict,
        "shapeValid": shape_valid,
    }


# ============================================================
# Odoo manufacturing feasibility gate + auto-select walk (PRIVATE)
# ============================================================

def _outcome(status: str, *, recommendation_id: str | None = None, preview_url: str | None = None, safe: CustomerSafeRecommendation | None = None, legacy_text: str = "") -> dict:
    return {
        "ok": status == STATUS_READY,
        "status": status,
        "recommendationId": recommendation_id,
        "previewUrl": preview_url,
        "safeRecommendation": safe,
        # Legacy text for the private dispatcher only (never model-visible).
        "modelContent": legacy_text or STATUS_GUIDANCE.get(status, ""),
        "sseEvent": {"type": "preview_ready", "recommendationId": recommendation_id, "previewId": recommendation_id, "previewUrl": preview_url} if status == STATUS_READY else None,
    }


async def _auto_select_and_confirm_best(session: AsyncSession, with_ids: list[dict], conversation_id: str, context: dict, likes: Any = None) -> dict:
    _log_preview_event("RECOMMENDATIONS_RANKED", conversation_id=conversation_id, recommendation_id=None, preview_id=None, event_type="generate_new_product_combinations", preview_url=None)

    def _reject(candidate_index: int, candidate: dict, stage: str, reason: str, inventory: dict | None = None) -> None:
        logger.info("AUTOSELECT_CANDIDATE_REJECTED %s", json.dumps({
            "conversationId": conversation_id, "recommendationId": candidate.get("recommendationId"), "candidateIndex": candidate_index,
            "inventoryValidated": (inventory or {}).get("inventoryValidated"), "buildable": (inventory or {}).get("buildable"),
            "rejectionStage": stage, "rejectionReason": reason,
        }))

    eligible_candidate_count = 0
    buildable_candidate_count = 0
    any_confidence_gated = False
    any_inventory_rejected = False
    any_identity_missing = False
    other_rejection_reason = None

    for candidate_index, candidate in enumerate(with_ids):
        if not candidate.get("autoConfirmEligible"):
            any_confidence_gated = True
            _reject(candidate_index, candidate, "confidence_gate", ",".join(candidate.get("autoConfirmReasons") or ["not autoConfirmEligible"]))
            continue
        eligible_candidate_count += 1

        inventory = await evaluate_candidate_inventory(session, candidate, candidate_index)
        logger.info("INVENTORY_CANDIDATE_RESULT %s", json.dumps({
            "conversationId": conversation_id, "recommendationId": candidate.get("recommendationId"), "candidateIndex": candidate_index,
            "inventoryValidated": inventory["inventoryValidated"], "buildable": inventory["buildable"],
            "limitingSku": inventory["limitingSku"], "maxBuildableBottles": inventory["maxBuildableBottles"],
        }))

        from app.services.inventory_snapshot import save_inventory_snapshot

        try:
            await save_inventory_snapshot(
                session, recommendation_id=candidate["recommendationId"], inventory_validated=inventory["inventoryValidated"],
                buildable=inventory["buildable"], checked_at=utcnow(), oil_total_ml=inventory["oilTotalMl"],
                alcohol_ml=inventory["alcoholMl"], request_status=inventory["status"],
                max_buildable_bottles=inventory["maxBuildableBottles"], limiting_sku=inventory["limitingSku"],
                components=inventory["components"],
            )
        except Exception as err:
            logger.error("Failed to save inventory snapshot: %s", type(err).__name__)
            # A failed flush leaves a SQLAlchemy session's transaction inactive until rolled back
            # -- unlike Prisma, where one failed create() doesn't poison later queries on the same
            # client. Rolling back here is what makes the "log and keep going" contract below
            # actually match JS's real behavior, not just its surface code shape.
            await session.rollback()

        if not inventory["buildable"]:
            any_inventory_rejected = True
            _reject(candidate_index, candidate, "inventory", inventory.get("limitingSku") or "not buildable from current inventory", inventory)
            continue
        buildable_candidate_count += 1

        confirm_result = await confirm_recommendation(
            session, recommendation_id=candidate["recommendationId"], customer_name=context.get("customerName"), customer_email=context.get("customerEmail")
        )
        if not confirm_result["ok"]:
            reason_code = confirm_result.get("reasonCode") or "unknown"
            _reject(candidate_index, candidate, f"confirmation:{reason_code}", confirm_result.get("reason", ""), inventory)
            # identity_missing is a systemic gate (checked before any candidate-specific logic in
            # confirm_recommendation) -- every remaining buildable candidate will fail it identically,
            # so it must never be masked by whatever generic reason the loop would otherwise settle on.
            if reason_code == "identity_missing":
                any_identity_missing = True
            else:
                other_rejection_reason = other_rejection_reason or confirm_result.get("reason")
            continue

        await save_customer_profile_fields(session, conversation_id, {"selectedRecommendationId": candidate["recommendationId"]})
        # Phase 1 (security): mint the build capability that authorizes this customer's browser
        # to open the preview and mutate this one build. If minting fails (e.g. the
        # BuildCapability table is missing) the flow fails closed -- no preview URL.
        build_token = await issue_build_token(session, recommendation_id=candidate["recommendationId"], conversation_id=conversation_id, shop=context["shopDomain"])
        preview_url = build_preview_url(context["shopDomain"], candidate["recommendationId"], build_token)
        _log_preview_event("BEST_RECOMMENDATION_SELECTED", conversation_id=conversation_id, recommendation_id=candidate["recommendationId"], preview_id=candidate["recommendationId"], event_type="preview_ready", preview_url=preview_url)
        _log_preview_event("PREVIEW_READY_EMITTED", conversation_id=conversation_id, recommendation_id=candidate["recommendationId"], preview_id=candidate["recommendationId"], event_type="preview_ready", preview_url=preview_url)
        safe = build_customer_safe_recommendation_from_candidate(candidate, inventory=inventory, likes=likes)
        grounded_facts = {
            "type": candidate.get("type"),
            "whySuits": candidate.get("customerFacingWhySuits"),
            "bestUse": candidate.get("customerFacingBestUse"),
            "strength": candidate.get("customerFacingStrength"),
            "weatherSuitability": candidate.get("customerFacingWeatherSuitability"),
            "risk": candidate.get("customerFacingRisk"),
        }
        legacy_text = (
            "The best recommendation was selected and confirmed automatically. Real grounded facts about "
            f"it, and only these: {json.dumps(grounded_facts)}. The preview page is opening on its own right now. In this "
            "reply, write ONE short, warm reasoning bridge (2-3 sentences) that connects 2-3 real details the customer "
            "actually told you earlier in this conversation to 2-3 real characteristics of this selected blend from the "
            "facts above -- grounded only in those, never invented. Then stop. Do NOT list multiple combinations, do NOT "
            "ask the customer to pick one, do NOT ask \"how do these sound\", do NOT ask for confirmation of any kind."
        )
        return _outcome(STATUS_READY, recommendation_id=candidate["recommendationId"], preview_url=preview_url, safe=safe, legacy_text=legacy_text)

    # Priority matters: identity_missing is a systemic gate that fails every remaining buildable
    # candidate identically (it's checked before any candidate-specific logic in
    # confirm_recommendation) -- it must never be reported as an availability/inventory problem,
    # which is a real, different, and misleading claim when buildable candidates actually exist.
    if any_identity_missing:
        failure_reason = "identity_missing"
    elif any_confidence_gated:
        failure_reason = "confidence_gated"
    elif any_inventory_rejected:
        failure_reason = "inventory_rejected"
    else:
        failure_reason = "verification_failed"

    logger.info("AUTOSELECT_FAILED %s", json.dumps({
        "conversationId": conversation_id, "reason": failure_reason,
        "eligibleCandidateCount": eligible_candidate_count, "buildableCandidateCount": buildable_candidate_count,
    }))

    if any_identity_missing:
        return _outcome(STATUS_IDENTITY_NEEDED, legacy_text="a real, buildable combination exists, but the customer's account name and email aren't available yet to attach it to. Do NOT claim this is a stock shortage or that we're waiting on inventory/supply -- that would be false, real buildable stock exists. Tell the customer honestly that we need their Shopify account signed in (with name and email available) before locking in a build, and ask them to make sure they're signed in.")
    if any_confidence_gated:
        return _outcome(STATUS_NEEDS_MORE_DETAIL, legacy_text="every generated combination was too low-confidence to recommend with certainty (thin evidence, weak fit to what the customer said, or a real compatibility risk) — tell the customer honestly that nothing felt like a confident enough match yet, and ask a bit more about their preferences rather than presenting a weak guess as a solid recommendation.")
    if any_inventory_rejected:
        return _outcome(STATUS_TEMPORARILY_UNAVAILABLE, legacy_text="every generated combination that otherwise fit the customer well couldn't be confirmed as buildable from current inventory — tell the customer honestly that we need a moment to find an available option, and offer to try again shortly.")
    return _outcome(STATUS_TEMPORARILY_UNAVAILABLE, legacy_text=f"every generated combination failed re-verification (catalog changed, ratio drift, or a dislike conflict — {other_rejection_reason or 'reason unavailable'}) — tell the customer there was a temporary issue preparing their fragrance and ask if they'd like to try again.")


async def run_generate(session: AsyncSession, conversation_id: str, context: dict, *, maximum_results: int = 8, allowed_types: list[str] | None = None) -> dict:
    """PRIVATE: the full candidate-analysis -> generation -> gating -> inventory -> confirmation
    walk. Returns an outcome dict (see _outcome). Never model-visible."""
    profile = await get_customer_profile(session, conversation_id)
    # Deterministic in Python, not left to the model: the discovery-completeness gate always runs.
    missing = get_missing_required_fields(profile)
    if missing:
        return {**_outcome(STATUS_NEEDS_MORE_DETAIL, legacy_text=f"not enough signal to generate yet -- still missing: {missing[0]}. Ask a natural follow-up to learn this before calling this tool again."), "ok": False, "missing": missing}
    mark_generation_attempted(conversation_id, profile)
    queried_profile = {**profile, "season": _effective_query_season(profile)}
    scratch = _get_scratch(conversation_id, profile)
    if not scratch["candidateProducts"]:
        scratch["candidateProducts"] = await analyze_customer_product_candidates(session, queried_profile)

    combinations = await generate_new_product_combinations(
        session, profile=queried_profile, candidate_products=scratch["candidateProducts"],
        maximum_results=maximum_results or 8, allowed_types=allowed_types,
    )
    for c in combinations:
        c.update(evaluate_auto_confirm_eligibility(c, queried_profile))
    recommendation_ids = [await save_recommendation(session, conversation_id=conversation_id, profile=profile, combination=c) for c in combinations]
    with_ids = [{"recommendationId": rid, **c} for rid, c in zip(recommendation_ids, combinations)]
    scratch["lastCombinations"] = with_ids
    if not with_ids:
        # Parity with the pre-Phase-3 handler: "nothing new to propose" was a non-error result
        # (with an empty combination_recommendations event) for the private dispatcher.
        return {**_outcome(STATUS_NEEDS_MORE_DETAIL, legacy_text="No genuinely new combinations could be generated from the current candidates — every viable pairing already exists, or none had a clear complementary role."), "legacyOk": True, "sseEvent": {"type": "combination_recommendations", "combinations": []}}

    return await _auto_select_and_confirm_best(session, with_ids, conversation_id, context, likes=profile.get("likes"))


async def run_refine(session: AsyncSession, conversation_id: str, context: dict, feedback: str) -> dict:
    """PRIVATE: refinement walk. Never model-visible."""
    profile = await get_customer_profile(session, conversation_id)
    queried_profile = {**profile, "season": _effective_query_season(profile)}
    scratch = _get_scratch(conversation_id, profile)
    if not scratch["candidateProducts"]:
        scratch["candidateProducts"] = await analyze_customer_product_candidates(session, queried_profile)

    current_notes: list[str] = []
    if profile.get("selectedRecommendationId"):
        current_recommendation = await session.scalar(
            select(FragranceRecommendation).where(FragranceRecommendation.id == profile["selectedRecommendationId"])
        )
        if current_recommendation:
            current_notes = [n for p in (current_recommendation.productsJson or []) for n in (p.get("notes") or [])]

    adjustments = derive_refinement_adjustments(feedback, current_notes)
    updated_likes = list(dict.fromkeys([*(profile.get("likes") or []), *adjustments["addLikeTerms"]]))
    updated_dislikes = list(dict.fromkeys([*(profile.get("dislikes") or []), *adjustments["addDislikeTerms"]]))
    if adjustments["addLikeTerms"] or adjustments["addDislikeTerms"]:
        await save_customer_profile_fields(session, conversation_id, {"likes": updated_likes, "dislikes": updated_dislikes})
    adjusted_profile = {**queried_profile, "likes": updated_likes, "dislikes": updated_dislikes}
    mark_generation_attempted(conversation_id, adjusted_profile)

    hard_exclude_families = text_to_preference_families(adjustments["addDislikeTerms"])
    combinations = await generate_new_product_combinations(
        session, profile=adjusted_profile, candidate_products=scratch["candidateProducts"],
        allowed_types=adjustments["allowedTypes"], hard_exclude_families=hard_exclude_families,
        hard_exclude_terms=adjustments["addDislikeTerms"],
    )
    for c in combinations:
        c.update(evaluate_auto_confirm_eligibility(c, adjusted_profile))
    recommendation_ids = [await save_recommendation(session, conversation_id=conversation_id, profile=adjusted_profile, combination=c) for c in combinations]
    with_ids = [{"recommendationId": rid, **c} for rid, c in zip(recommendation_ids, combinations)]
    scratch["lastCombinations"] = with_ids
    if not with_ids:
        return {**_outcome(STATUS_NEEDS_MORE_DETAIL, legacy_text=f'No genuinely new combinations could be generated from "{feedback}" — every viable pairing already exists, or none had a clear complementary role.'), "legacyOk": True, "sseEvent": {"type": "combination_recommendations", "combinations": []}}

    return await _auto_select_and_confirm_best(session, with_ids, conversation_id, context, likes=updated_likes)


# ============================================================
# MODEL-FACING dispatcher (least privilege)
# ============================================================

def _ok(model_content: str, sse_event: dict | None = None) -> dict:
    return {"modelContent": model_content, "sseEvent": sse_event}


def _fail(message: str) -> dict:
    return {"modelContent": f"Error: {message}", "sseEvent": None}


def _safe_recommendation_tool_result(outcome: dict) -> dict:
    """What the model learns from a refinement: the customer-safe recommendation (no ids) or a
    status label with server-authored guidance."""
    if outcome["status"] == STATUS_READY:
        safe = outcome["safeRecommendation"]
        content = json.dumps({
            "recommendation": safe.model_dump(),
            "instruction": "The preview page is opening on its own. Write ONE short, warm bridge (2-3 sentences) connecting what the customer told you to this fragrance's character, notes, and fit. Do not list options, do not ask them to choose, do not ask for confirmation.",
        }, ensure_ascii=False)
        return _ok(content, outcome["sseEvent"])
    return _ok(json.dumps({"status": outcome["status"], "instruction": STATUS_GUIDANCE.get(outcome["status"], "")}), None)


async def execute_model_tool(session: AsyncSession, tool_name: str, raw_args_json: str, context: dict, *, allowed_tool_names) -> dict:
    """The only entry point the conversational model can reach. Refuses everything that is not a
    model-callable tool, validates arguments strictly, and returns customer-safe content only.

    Phase 4A: `allowed_tool_names` is REQUIRED and is this turn's permission set (from
    security_gate.TurnPermissions). A tool that exists in the global allowlist but was not
    permitted for this turn is refused here, before dispatch -- whatever the model returned and
    whatever was (or was not) offered to it. `tools=None` on the request is not a boundary."""
    if tool_name not in MODEL_CALLABLE_TOOL_NAMES or tool_name not in frozenset(allowed_tool_names or ()):
        logger.info("MODEL_TOOL_REFUSED %s", json.dumps({"conversationId": context.get("conversationId"), "tool": str(tool_name)[:60]}))
        return _fail("that action is not available.")
    try:
        raw_args = json.loads(raw_args_json) if raw_args_json else {}
    except (json.JSONDecodeError, TypeError):
        return _fail(f"couldn't parse arguments as JSON — call {tool_name} again with valid JSON.")
    try:
        args = validate_model_tool_arguments(tool_name, raw_args)
    except ModelToolArgumentError as err:
        return _fail(str(err))

    try:
        if tool_name == "save_customer_profile_field":
            return await _handle_save_customer_profile_field(session, context["conversationId"], args, context)
        if tool_name == "verify_customer_location":
            return await _handle_verify_customer_location(session, context["conversationId"], args)
        if tool_name == "resolve_season_preference":
            return await _handle_resolve_season_preference(session, context["conversationId"], args)
        if tool_name == "refine_fragrance_recommendation":
            outcome = await run_refine(session, context["conversationId"], context, args["feedback"])
            return _safe_recommendation_tool_result(outcome)
    except Exception as err:
        logger.error("model tool %s failed: %s", tool_name, type(err).__name__, exc_info=True)
        return _fail(f"internal error while running {tool_name}.")
    return _fail("that action is not available.")


# ============================================================
# PRIVATE dispatcher (server-side pipeline and tests only)
# ============================================================

async def execute_fragrance_tool(session: AsyncSession, tool_name: str, raw_args_json: str, context: dict) -> dict:
    """context: {"conversationId", "customerName", "customerEmail", "shopDomain"}. Results may
    contain internal data and must never be placed in model context."""
    conversation_id = context["conversationId"]

    try:
        args = json.loads(raw_args_json) if raw_args_json else {}
    except (json.JSONDecodeError, TypeError):
        return _fail(f"couldn't parse arguments as JSON — call {tool_name} again with valid JSON.")

    try:
        if tool_name == "save_customer_profile_field":
            return await _handle_save_customer_profile_field(session, conversation_id, args, context)

        if tool_name == "get_customer_profile":
            profile = await get_customer_profile(session, conversation_id)
            missing = get_missing_required_fields(profile)
            return _ok(f"Current profile: {json.dumps(profile)}\nMissing required fields: {', '.join(missing) or 'none'}.")

        if tool_name == "verify_customer_location":
            return await _handle_verify_customer_location(session, conversation_id, args)

        if tool_name == "resolve_season_preference":
            return await _handle_resolve_season_preference(session, conversation_id, args)

        if tool_name == "select_recommendation":
            return await _handle_select_recommendation(session, conversation_id, args)

        if tool_name == "analyze_customer_product_candidates":
            return await _handle_analyze_candidates(session, conversation_id)

        if tool_name == "get_product_notes_and_combination_status":
            product_title = args.get("productTitle")
            if not product_title:
                return _fail("productTitle is required")
            result = await get_product_notes_and_combination_status(session, product_title)
            return _ok(json.dumps(result))

        if tool_name == "find_existing_combinations_for_product":
            product_title = args.get("productTitle")
            if not product_title:
                return _fail("productTitle is required")
            result = await find_existing_combinations_for_product(session, product_title)
            return _ok(json.dumps(result))

        if tool_name == "check_exact_combination_exists":
            titles = args.get("productTitles")
            if not isinstance(titles, list) or not (2 <= len(titles) <= 4):
                return _fail("productTitles must be an array of 2-4 titles")
            result = await check_exact_combination_exists(session, titles)
            return _ok(json.dumps(result))

        if tool_name == "find_combinations_using_similar_notes":
            product_title = args.get("productTitle")
            if not product_title:
                return _fail("productTitle is required")
            result = await find_combinations_using_similar_notes(session, product_title, args.get("limit") or 5)
            return _ok(json.dumps(result))

        if tool_name == "generate_new_product_combinations":
            outcome = await run_generate(session, conversation_id, context, maximum_results=args.get("maximumResults") or 8, allowed_types=args.get("allowedTypes"))
            return _ok(outcome["modelContent"], outcome.get("sseEvent")) if (outcome["ok"] or outcome.get("legacyOk")) else _fail(outcome["modelContent"])

        if tool_name in ("refine_combination_recommendations", "refine_fragrance_recommendation"):
            feedback = args.get("feedback")
            if not feedback:
                return _fail("feedback is required")
            outcome = await run_refine(session, conversation_id, context, feedback)
            return _ok(outcome["modelContent"], outcome.get("sseEvent")) if (outcome["ok"] or outcome.get("legacyOk")) else _fail(outcome["modelContent"])

        if tool_name == "confirm_product_combination":
            return await _handle_confirm_product_combination(session, conversation_id, args, context)

        return _fail(f'unknown tool "{tool_name}".')
    except Exception as err:
        logger.error("fragranceAgentTools: %s failed: %s", tool_name, type(err).__name__, exc_info=True)
        return _fail(f"internal error while running {tool_name}.")


async def _handle_save_customer_profile_field(session: AsyncSession, conversation_id: str, args: dict, context: dict) -> dict:
    from app.ai.tools import PROFILE_FIELD_NAMES

    field = args.get("field")
    value = args.get("value")
    if field not in PROFILE_FIELD_NAMES:
        return _fail(f'"field" must be one of {PROFILE_FIELD_NAMES}')
    if not isinstance(value, (str, list, bool)):
        return _fail('"value" must be a string, array of strings, or boolean')

    # A trusted identity (authenticated Shopify account, or already saved on the profile) can
    # never be overwritten by a model-supplied value.
    if field == "name" and context.get("customerName"):
        return _ok("Name is already known — no need to save or ask again.")
    if field == "email" and context.get("customerEmail"):
        return _ok("Email is already known — no need to save or ask again.")
    # Phase 4 (F5, profile poisoning): a value that reads like an instruction to the assistant
    # (role override, prompt/tool extraction, authority claim, encoded payload) is never stored,
    # whichever model or extraction path proposed it. The model is told nothing beyond "not saved".
    from app.ai.security_gate import looks_like_instruction

    candidates = value if isinstance(value, list) else [value]
    if any(isinstance(v, str) and looks_like_instruction(v) for v in candidates):
        logger.warning("SECURITY_PROFILE_WRITE_REJECTED %s", json.dumps({"conversationId": conversation_id, "field": field}))
        return _fail("that value was not saved. Continue the fragrance conversation.")
    if field == "name" and isinstance(value, str) and _is_implausible_name(value):
        return _fail('that doesn\'t read like a real name — do not save it. They likely answered a different question, or their reply got misread as an answer to "what should I call you?" Gently ask for their name again instead of guessing.')

    vocabulary_corrections: list[dict] = []
    if field in _VOCABULARY_CORRECTED_FIELDS:
        if isinstance(value, list):
            result = correct_preference_vocabulary_list(value)
            value = result["corrected"]
            vocabulary_corrections = result["corrections"]
        elif isinstance(value, str):
            result = correct_preference_vocabulary(value)
            value = result["corrected"]
            vocabulary_corrections = result["corrections"]

    if field == "dislikes" and isinstance(value, list):
        value = [v for v in value if not _NO_REAL_VALUE_PATTERN.match(str(v).strip())]

    if field == "preferredStyle" and isinstance(value, str) and _NO_REAL_STYLE_PATTERN.search(value):
        current = await get_customer_profile(session, conversation_id)
        inferred = _infer_style_from_profile(current)
        profile = await save_customer_profile_fields(session, conversation_id, {"inferredStyle": inferred})
        missing = get_missing_required_fields(profile)
        return _ok(
            f'The customer didn\'t give a real style preference — inferred "{inferred}" from their other stated signals and saved it as inferredStyle (not a literal quote from them). Missing required fields: {", ".join(missing) if missing else "none — ready to analyze."}',
            {"type": "profile_progress", "profile": profile, "missingFields": missing},
        )

    ok, coerced_or_error = validate_profile_field_value(field, value)
    if not ok:
        return _fail(coerced_or_error)
    value = coerced_or_error

    fields_to_save: dict[str, Any] = {field: value}
    if vocabulary_corrections:
        current_profile = await get_customer_profile(session, conversation_id)
        fields_to_save["preferenceVocabularyCorrections"] = [
            *(current_profile.get("preferenceVocabularyCorrections") or []),
            *({"field": field, **c} for c in vocabulary_corrections),
        ][-50:]  # Phase 2: bounded (most recent 50), never an unbounded blob
    profile = await save_customer_profile_fields(session, conversation_id, fields_to_save)
    missing = get_missing_required_fields(profile)

    if field == "requestedSeasonStyle":
        conflict = has_season_weather_conflict(profile.get("requestedSeasonStyle"), profile.get("weatherDirection")) and not profile.get("seasonStyleConflictResolved")
        base = f'Saved. Missing required fields before analysis: {", ".join(missing) if missing else "none — ready to analyze."}'
        if conflict:
            style_lower = profile["requestedSeasonStyle"].lower()
            city = profile.get("city") or "your city"
            extra = (
                f' Real weather today is "{profile["weatherDirection"]}", which conflicts with the requested {profile["requestedSeasonStyle"]} style — briefly ask the customer ONCE whether to keep that style anyway or base it on today\'s real conditions, then call resolve_season_preference with their answer. Do not present this as a rigid either/or menu — a light check-in, e.g. "It\'s mild and sunny in {city} today, but I can still shape it with a deeper {style_lower}-style character. Should I keep that direction?"'
            )
        else:
            extra = " No real conflict with today's weather — do not mention season at all, just continue naturally."
        return _ok(base + extra, {"type": "profile_progress", "profile": profile, "missingFields": missing})

    if field in ("name", "email"):
        # Identity fields never factor into recommendation readiness (get_missing_required_fields
        # only looks at fragrance-preference signals) -- appending "missing before analysis" here
        # was a live bug: right after the model saved a bare name during small talk, this line put
        # "likes or preferredStyle" in front of it as the very next thing to do, and the model
        # jumped straight to a fragrance question despite the system prompt explicitly saying not
        # to. Saving identity has nothing to do with fragrance readiness, so don't imply it does.
        return _ok("Saved.", {"type": "profile_progress", "profile": profile, "missingFields": missing})

    return _ok(
        f'Saved. Missing required fields before analysis: {", ".join(missing) if missing else "none — ready to analyze."}',
        {"type": "profile_progress", "profile": profile, "missingFields": missing},
    )


async def _handle_verify_customer_location(session: AsyncSession, conversation_id: str, args: dict) -> dict:
    city_text = args.get("cityText")
    if not city_text:
        return _fail("cityText is required")
    result = await verify_city(session, city_text)
    if result["needsClarification"]:
        return _ok(f'Multiple real places match that city: {json.dumps(result["candidates"])}. Ask the customer which one they mean.')
    if not result["verified"]:
        return _ok("I couldn't confidently match that location. Which real city are you currently in?")

    prior_profile = await get_customer_profile(session, conversation_id)
    fields: dict[str, Any] = {
        "city": result["city"], "country": result["country"] or prior_profile.get("country"),
        "stateRegion": result.get("stateRegion") or prior_profile.get("stateRegion"),
        "locationVerified": True, "locationSource": result["source"],
    }

    conflict_message = ""
    weather = await fetch_current_weather(result["city"], result.get("latitude"), result.get("longitude"))
    if weather:
        summary = describe_weather_simple(weather["tempF"], weather["weatherCode"])["summary"]
        direction = derive_weather_direction(weather["tempF"], weather["weatherCode"], weather["relativeHumidityPercent"])
        fields["currentWeather"] = {
            "condition": summary, "temperatureC": round((weather["tempF"] - 32) * 5 / 9), "fetchedAt": utcnow().isoformat(),
        }
        fields["weatherDirection"] = direction
        fields["weatherLocation"] = {"city": result["city"], "country": fields["country"], "verified": True}

        if prior_profile.get("requestedSeasonStyle") and not prior_profile.get("seasonStyleConflictResolved") and has_season_weather_conflict(prior_profile["requestedSeasonStyle"], direction):
            conflict_message = f' Real weather in {result["city"]} is "{direction}", which conflicts with the previously requested {prior_profile["requestedSeasonStyle"]} style — briefly ask the customer ONCE whether to keep that style or base it on today\'s real conditions, then call resolve_season_preference.'

    profile = await save_customer_profile_fields(session, conversation_id, fields)
    return _ok(
        f'Verified "{result["city"]}" ({result["country"] or "country unknown"}). Weather fetched and saved automatically — never ask the customer what season it is, never explain that recommendations will be adjusted "accordingly"; just continue naturally (e.g. into preferences/dislikes).{conflict_message}',
        {"type": "profile_progress", "profile": profile, "missingFields": get_missing_required_fields(profile)},
    )


async def _handle_resolve_season_preference(session: AsyncSession, conversation_id: str, args: dict) -> dict:
    choice = args.get("choice")
    if choice not in ("keep_style", "use_weather"):
        return _fail('choice must be "keep_style" or "use_weather"')
    current = await get_customer_profile(session, conversation_id)
    profile = await save_customer_profile_fields(session, conversation_id, {
        "requestedSeasonStyle": current.get("requestedSeasonStyle") if choice == "keep_style" else None,
        "seasonStyleConflictResolved": True,
    })
    if choice == "keep_style":
        message = f"Resolved. Keeping the requested {profile['requestedSeasonStyle']} style as the basis. Never ask about this again."
    else:
        message = f"Resolved. Basing the recommendation on today's real weather ({profile['weatherDirection']}) instead. Never ask about this again."
    return _ok(message, {"type": "profile_progress", "profile": profile, "missingFields": get_missing_required_fields(profile)})


async def _handle_select_recommendation(session: AsyncSession, conversation_id: str, args: dict) -> dict:
    selection_text = args.get("selectionText")
    if not selection_text:
        return _fail("selectionText is required")
    scratch = _get_scratch(conversation_id)
    active_list = scratch["lastCombinations"] or []

    if not active_list:
        pending = list((
            await session.execute(
                select(FragranceRecommendation)
                .where(FragranceRecommendation.conversationId == conversation_id, FragranceRecommendation.status == "pending")
                .order_by(FragranceRecommendation.createdAt.desc())
                .limit(10)
            )
        ).scalars())
        active_list = [{"recommendationId": r.id} for r in reversed(pending)]
        scratch["lastCombinations"] = active_list

    result = parse_recommendation_selection(selection_text, active_list)
    if result.get("noMatch"):
        return _fail("couldn't tell which recommendation the customer means — ask one short clarifying question (e.g. 'Do you mean option 1, 2, or 3?').")
    if result.get("ambiguous"):
        return _fail("the selection was ambiguous — ask the customer to confirm which option number they mean.")
    profile = await save_customer_profile_fields(session, conversation_id, {"selectedRecommendationId": result["recommendationId"]})
    return _ok(
        f"Selected recommendationId {result['recommendationId']}. This is now the customer's chosen recommendation — never substitute a different one.",
        {"type": "recommendation_selected", "recommendationId": result["recommendationId"], "profile": profile},
    )


def _redact_titles_for_sse(candidates: list[dict]) -> list[dict]:
    # Kept for the private dispatcher's SSE payload; the public route additionally strips every
    # field of this event type (app/api/chat.py allowlist).
    return [{k: v for k, v in c.items() if k not in ("productName", "normalizedProductName")} for c in candidates]


async def _handle_analyze_candidates(session: AsyncSession, conversation_id: str) -> dict:
    profile = await get_customer_profile(session, conversation_id)
    missing = get_missing_required_fields(profile)
    if missing:
        return _fail(f"not enough signal to analyze yet -- still missing: {missing[0]}. Ask a natural follow-up to learn this before calling this tool again.")
    candidate_products = await analyze_customer_product_candidates(session, {**profile, "season": _effective_query_season(profile)})
    _get_scratch(conversation_id, profile)["candidateProducts"] = candidate_products
    if not candidate_products:
        return _ok(
            "No real product candidates found for this profile yet — there may be limited historical data for this exact region/season combination.",
            {"type": "analysis_progress", "candidateProducts": []},
        )
    return _ok(
        f"Real product candidates (highest relevance first): {json.dumps(candidate_products)}\n\n"
        "Call generate_new_product_combinations now to build real Hybrid/Tribrid/Quadbrid combinations from these candidates -- do not stop here.",
        {"type": "candidate_products", "candidateProducts": _redact_titles_for_sse(candidate_products)},
    )


async def _handle_confirm_product_combination(session: AsyncSession, conversation_id: str, args: dict, context: dict) -> dict:
    recommendation_id_arg = args.get("recommendationId")
    if not recommendation_id_arg:
        return _fail("recommendationId is required")
    profile = await get_customer_profile(session, conversation_id)
    recommendation_id = profile.get("selectedRecommendationId") or recommendation_id_arg
    if profile.get("selectedRecommendationId") and recommendation_id_arg != profile["selectedRecommendationId"]:
        return _fail(f"the customer's selected recommendation is {profile['selectedRecommendationId']} — use that one, never a different id.")

    result = await confirm_recommendation(session, recommendation_id=recommendation_id, customer_name=context.get("customerName"), customer_email=context.get("customerEmail"))
    if not result["ok"]:
        return _fail(f"{result['reason']} Keep the customer's selected recommendation unchanged — do not propose a different one. You may retry confirm_product_combination with the same recommendationId.")

    build_token = await issue_build_token(session, recommendation_id=recommendation_id, conversation_id=conversation_id, shop=context["shopDomain"])
    legacy_preview_url = build_preview_url(context["shopDomain"], recommendation_id, build_token)
    _log_preview_event("PREVIEW_READY_EMITTED", conversation_id=conversation_id, recommendation_id=recommendation_id, preview_id=recommendation_id, event_type="preview_ready", preview_url=legacy_preview_url)
    return _ok(
        "Confirmed. Tell the customer their fragrance preview is ready — do NOT say a product has been created yet. The frontend will open the preview page automatically.",
        {"type": "preview_ready", "recommendationId": recommendation_id, "previewId": recommendation_id, "previewUrl": legacy_preview_url},
    )
