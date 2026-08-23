"""Port of app/tools/fragranceAgentTools.server.js -- the 13 fragrance-agent tools in OpenAI
function-calling format, and execute_fragrance_tool(), the single dispatch point the conversation
loop's tool-call loop invokes. Every handler talks to real data only -- no handler ever invents a
product, note, score, or ratio.
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
from app.ai.tools import validate_profile_field_value
from app.db.models import FragranceRecommendation
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
from app.db.time import utcnow

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


def _compute_profile_hash(profile: dict) -> str:
    relevant = {
        "city": profile.get("city"), "stateRegion": profile.get("stateRegion"), "country": profile.get("country"),
        "requestedSeasonStyle": profile.get("requestedSeasonStyle"), "weatherDirection": profile.get("weatherDirection"),
        "likes": profile.get("likes"), "dislikes": profile.get("dislikes"), "preferredStyle": profile.get("preferredStyle"),
        "inferredStyle": profile.get("inferredStyle"), "additionalPreferences": profile.get("additionalPreferences"),
        "strengthPreference": profile.get("strengthPreference"), "occasion": profile.get("occasion"),
    }
    return hashlib.sha256(json.dumps(relevant, sort_keys=False).encode()).hexdigest()


def _log_preview_event(stage: str, *, conversation_id, recommendation_id, preview_id, event_type, preview_url) -> None:
    logger.info("%s %s", stage, json.dumps({
        "conversationId": conversation_id, "recommendationId": recommendation_id,
        "previewId": preview_id, "eventType": event_type, "previewUrl": preview_url,
    }))


# Ephemeral, regenerable per-conversation scratch space -- NOT the durable CustomerProfileState.
# Process-global (lost on restart), exactly like the JS module-level Map.
_conversation_scratch: dict[str, dict[str, Any]] = {}


def _get_scratch(conversation_id: str, profile: dict | None = None) -> dict:
    if conversation_id not in _conversation_scratch:
        _conversation_scratch[conversation_id] = {"candidateProducts": None, "lastCombinations": None, "profileHash": None}
    scratch = _conversation_scratch[conversation_id]
    if profile:
        h = _compute_profile_hash(profile)
        if scratch["profileHash"] and scratch["profileHash"] != h:
            scratch["candidateProducts"] = None
            scratch["lastCombinations"] = None
        scratch["profileHash"] = h
    return scratch


def get_scratch_for_testing(conversation_id: str) -> dict | None:
    return _conversation_scratch.get(conversation_id)


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
# Odoo manufacturing feasibility gate + auto-select walk
# ============================================================

async def _auto_select_and_confirm_best(session: AsyncSession, with_ids: list[dict], conversation_id: str, context: dict) -> dict:
    _log_preview_event("RECOMMENDATIONS_RANKED", conversation_id=conversation_id, recommendation_id=None, preview_id=None, event_type="generate_new_product_combinations", preview_url=None)

    any_confidence_gated = False
    any_inventory_rejected = False

    for candidate_index, candidate in enumerate(with_ids):
        if not candidate.get("autoConfirmEligible"):
            any_confidence_gated = True
            continue

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
            logger.error("Failed to save inventory snapshot: %s", err)
            # A failed flush leaves a SQLAlchemy session's transaction inactive until rolled back
            # -- unlike Prisma, where one failed create() doesn't poison later queries on the same
            # client. Rolling back here is what makes the "log and keep going" contract below
            # actually match JS's real behavior, not just its surface code shape.
            await session.rollback()

        if not inventory["buildable"]:
            any_inventory_rejected = True
            continue

        confirm_result = await confirm_recommendation(
            session, recommendation_id=candidate["recommendationId"], customer_name=context.get("customerName"), customer_email=context.get("customerEmail")
        )
        if not confirm_result["ok"]:
            continue

        await save_customer_profile_fields(session, conversation_id, {"selectedRecommendationId": candidate["recommendationId"]})
        preview_url = build_preview_url(context["shopDomain"], candidate["recommendationId"])
        _log_preview_event("BEST_RECOMMENDATION_SELECTED", conversation_id=conversation_id, recommendation_id=candidate["recommendationId"], preview_id=candidate["recommendationId"], event_type="preview_ready", preview_url=preview_url)
        _log_preview_event("PREVIEW_READY_EMITTED", conversation_id=conversation_id, recommendation_id=candidate["recommendationId"], preview_id=candidate["recommendationId"], event_type="preview_ready", preview_url=preview_url)
        return {
            "ok": True,
            "modelContent": f"The best recommendation (recommendationId {candidate['recommendationId']}) was selected and confirmed automatically. The preview page is opening on its own right now — do NOT list any combinations, do NOT ask the customer to pick one, do NOT ask \"how do these sound\", and do NOT say anything further about this turn.",
            "sseEvent": {"type": "preview_ready", "recommendationId": candidate["recommendationId"], "previewId": candidate["recommendationId"], "previewUrl": preview_url},
        }

    if any_confidence_gated:
        return {
            "ok": False,
            "modelContent": "every generated combination was too low-confidence to recommend with certainty (thin evidence, weak fit to what the customer said, or a real compatibility risk) — tell the customer honestly that nothing felt like a confident enough match yet, and ask a bit more about their preferences rather than presenting a weak guess as a solid recommendation.",
        }
    if any_inventory_rejected:
        return {
            "ok": False,
            "modelContent": "every generated combination that otherwise fit the customer well couldn't be confirmed as buildable from current inventory — tell the customer honestly that we need a moment to find an available option, and offer to try again shortly.",
        }
    return {
        "ok": False,
        "modelContent": "every generated combination failed re-verification (catalog changed, ratio drift, or a dislike conflict) — tell the customer there was a temporary issue preparing their fragrance and ask if they'd like to try again.",
    }


# ============================================================
# Dispatch
# ============================================================

def _ok(model_content: str, sse_event: dict | None = None) -> dict:
    return {"modelContent": model_content, "sseEvent": sse_event}


def _fail(message: str) -> dict:
    return {"modelContent": f"Error: {message}", "sseEvent": None}


async def execute_fragrance_tool(session: AsyncSession, tool_name: str, raw_args_json: str, context: dict) -> dict:
    """context: {"conversationId", "customerName", "customerEmail", "shopDomain"}."""
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
            return await _handle_generate_combinations(session, conversation_id, args, context)

        if tool_name == "refine_combination_recommendations":
            return await _handle_refine_combinations(session, conversation_id, args, context)

        if tool_name == "confirm_product_combination":
            return await _handle_confirm_product_combination(session, conversation_id, args, context)

        return _fail(f'unknown tool "{tool_name}".')
    except Exception as err:
        logger.error("fragranceAgentTools: %s failed: %s", tool_name, err, exc_info=True)
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
        return _ok(f"Name is already known and trusted ({context['customerName']}) — no need to save or ask again.")
    if field == "email" and context.get("customerEmail"):
        return _ok("Email is already known and trusted — no need to save or ask again.")
    if field == "name" and isinstance(value, str) and _is_implausible_name(value):
        return _fail(f'"{value}" doesn\'t read like a real name — do not save it. They likely answered a different question, or their reply got misread as an answer to "what should I call you?" Gently ask for their name again instead of guessing.')

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
        ]
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
        return _ok(f'Multiple real places match "{city_text}": {json.dumps(result["candidates"])}. Ask the customer which one they mean.')
    if not result["verified"]:
        return _ok("I couldn't confidently match that location. Which real city are you currently in?")

    prior_profile = await get_customer_profile(session, conversation_id)
    fields: dict[str, Any] = {
        "city": result["city"], "country": result["country"] or prior_profile.get("country"),
        "locationVerified": True, "locationSource": result["source"],
    }

    conflict_message = ""
    weather = await fetch_current_weather(result["city"])
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
        f'Verified "{result["city"]}" ({result["country"] or "country unknown"}) via {result["source"]}. Weather fetched and saved automatically — never ask the customer what season it is, never explain that recommendations will be adjusted "accordingly"; just continue naturally (e.g. into preferences/dislikes).{conflict_message}',
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


async def _handle_analyze_candidates(session: AsyncSession, conversation_id: str) -> dict:
    profile = await get_customer_profile(session, conversation_id)
    missing = get_missing_required_fields(profile)
    if missing:
        return _fail(f'profile is missing required fields ({", ".join(missing)}) — ask the customer for these before analyzing.')
    candidate_products = await analyze_customer_product_candidates(session, {**profile, "season": _effective_query_season(profile)})
    _get_scratch(conversation_id, profile)["candidateProducts"] = candidate_products
    if not candidate_products:
        return _ok(
            "No real product candidates found for this profile yet — there may be limited historical data for this exact region/season combination.",
            {"type": "analysis_progress", "candidateProducts": []},
        )
    return _ok(f"Real product candidates (highest relevance first): {json.dumps(candidate_products)}", {"type": "candidate_products", "candidateProducts": candidate_products})


async def _handle_generate_combinations(session: AsyncSession, conversation_id: str, args: dict, context: dict) -> dict:
    profile = await get_customer_profile(session, conversation_id)
    queried_profile = {**profile, "season": _effective_query_season(profile)}
    scratch = _get_scratch(conversation_id, profile)
    if not scratch["candidateProducts"]:
        scratch["candidateProducts"] = await analyze_customer_product_candidates(session, queried_profile)

    combinations = await generate_new_product_combinations(
        session, profile=queried_profile, candidate_products=scratch["candidateProducts"],
        maximum_results=args.get("maximumResults") or 8, allowed_types=args.get("allowedTypes"),
    )
    for c in combinations:
        c.update(evaluate_auto_confirm_eligibility(c, queried_profile))
    recommendation_ids = [await save_recommendation(session, conversation_id=conversation_id, profile=profile, combination=c) for c in combinations]
    with_ids = [{"recommendationId": rid, **c} for rid, c in zip(recommendation_ids, combinations)]
    scratch["lastCombinations"] = with_ids
    if not with_ids:
        return _ok(
            "No genuinely new combinations could be generated from the current candidates — every viable pairing already exists, or none had a clear complementary role.",
            {"type": "combination_recommendations", "combinations": []},
        )

    result = await _auto_select_and_confirm_best(session, with_ids, conversation_id, context)
    return _ok(result["modelContent"], result.get("sseEvent")) if result["ok"] else _fail(result["modelContent"])


async def _handle_refine_combinations(session: AsyncSession, conversation_id: str, args: dict, context: dict) -> dict:
    feedback = args.get("feedback")
    if not feedback:
        return _fail("feedback is required")
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
        return _ok(
            f'No genuinely new combinations could be generated from "{feedback}" — every viable pairing already exists, or none had a clear complementary role.',
            {"type": "combination_recommendations", "combinations": []},
        )

    result = await _auto_select_and_confirm_best(session, with_ids, conversation_id, context)
    return _ok(result["modelContent"], result.get("sseEvent")) if result["ok"] else _fail(result["modelContent"])


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

    legacy_preview_url = build_preview_url(context["shopDomain"], recommendation_id)
    _log_preview_event("PREVIEW_READY_EMITTED", conversation_id=conversation_id, recommendation_id=recommendation_id, preview_id=recommendation_id, event_type="preview_ready", preview_url=legacy_preview_url)
    return _ok(
        "Confirmed. Tell the customer their fragrance preview is ready — do NOT say a product has been created yet. The frontend will open the preview page automatically.",
        {"type": "preview_ready", "recommendationId": recommendation_id, "previewId": recommendation_id, "previewUrl": legacy_preview_url},
    )
