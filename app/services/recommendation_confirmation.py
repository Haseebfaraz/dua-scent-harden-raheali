"""Port of app/services/recommendationConfirmation.server.js -- save_recommendation /
confirm_product_combination. A FragranceRecommendation row is the immutable record the
confirmation tool re-verifies against; the model only ever passes a recommendationId, never a
free-form reconstructed product array.
"""

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.ids import new_id
from app.db.models import ExistingCombination, FragranceProduct, FragranceRecommendation
from app.db.time import utcnow
from app.fragrance.compatibility import literal_note_match_count, split_dislikes_by_exactness
from app.fragrance.normalization import normalize_product_name
from app.fragrance.scoring import classify_dislike_conflict
from app.services.recommendation_engine import validate_combination_shape

COMPONENT_COUNT_BY_TYPE = {"HYBRID": 2, "TRIBRID": 3, "QUADBRID": 4}
# No exact expiry window is specified beyond "has not expired" -- 24h is a deliberate, documented
# choice: long enough to survive a customer stepping away mid-conversation, short enough that a
# confirmation can't fire against a catalog that's since changed underneath it.
RECOMMENDATION_EXPIRY = timedelta(hours=24)


async def save_recommendation(session: AsyncSession, *, conversation_id: str, profile: dict, combination: dict) -> str:
    """Persists one generated proposal (from recommendation_engine.py) as an immutable,
    confirmable record. Re-runs the same hard shape gate the engine already applies at
    generation time, as defense in depth.
    """
    validate_combination_shape(combination["type"], combination["internalProducts"], combination.get("recommendedRatio"))

    recent_for_conversation = (
        await session.execute(
            select(FragranceRecommendation.id, FragranceRecommendation.evidenceJson)
            .where(
                FragranceRecommendation.conversationId == conversation_id,
                FragranceRecommendation.status.in_(["pending", "confirmed"]),
                FragranceRecommendation.createdAt >= utcnow() - RECOMMENDATION_EXPIRY,
            )
            .order_by(FragranceRecommendation.createdAt.desc())
        )
    ).all()
    # Guard against combination["canonicalKey"] itself being missing/falsy -- without this, two
    # genuinely different combinations that both lack one would collapse into a single row.
    canonical_key = combination.get("canonicalKey")
    existing_id = None
    if canonical_key:
        existing_id = next((rid for rid, evidence in recent_for_conversation if (evidence or {}).get("canonicalKey") == canonical_key), None)
    if existing_id:
        return existing_id

    now = utcnow()
    record = FragranceRecommendation(
        id=new_id(),
        conversationId=conversation_id,
        customerProfileJson=profile,
        productsJson=combination["internalProducts"],
        combinationType=combination["type"],
        scoreJson={
            "preferenceScore": combination.get("preferenceScore"),
            "seasonalScore": combination.get("seasonalScore"),
            "historyScore": combination.get("historyScore"),
            "compatibilityScore": combination.get("compatibilityScore"),
            "balanceScore": combination.get("balanceScore"),
            "conflictPenalty": combination.get("conflictPenalty"),
            "riskPenalty": combination.get("riskPenalty"),
            "riskBreakdown": combination.get("riskBreakdown"),
            "requestedExactNotes": combination.get("requestedExactNotes"),
            "matchedExactNotes": combination.get("matchedExactNotes"),
            "missingExactNotes": combination.get("missingExactNotes"),
            "exactNoteCoverageScore": combination.get("exactNoteCoverageScore"),
            "requestedPreferenceFamilies": combination.get("requestedPreferenceFamilies"),
            "matchedPreferenceFamilies": combination.get("matchedPreferenceFamilies"),
            "missingPreferenceFamilies": combination.get("missingPreferenceFamilies"),
            "floralRoleStrength": combination.get("floralRoleStrength"),
            "fallbackUsed": combination.get("fallbackUsed"),
            "fallbackTargetNotes": combination.get("fallbackTargetNotes"),
            "finalScore": combination.get("finalScore"),
            "confidence": combination.get("confidence"),
            "confidenceBreakdown": combination.get("confidenceBreakdown"),
            "customerFitScore": combination.get("customerFitScore"),
            "autoConfirmEligible": combination.get("autoConfirmEligible"),
            "autoConfirmReasons": combination.get("autoConfirmReasons"),
            "customerFitThreshold": combination.get("customerFitThreshold"),
            "highestCountedRiskSeverity": combination.get("highestCountedRiskSeverity"),
            "hasHardDislikeConflict": combination.get("hasHardDislikeConflict"),
            "shapeValid": combination.get("shapeValid"),
        },
        evidenceJson={
            "historicalEvidence": combination.get("historicalEvidence"),
            "analogousExistingCombinations": combination.get("analogousExistingCombinations"),
            "compatibilityReasons": combination.get("compatibilityReasons"),
            "risks": combination.get("risks"),
            "canonicalKey": combination.get("canonicalKey"),
        },
        ratiosJson=combination.get("recommendedRatio"),
        evidenceScope=combination.get("evidenceScope"),
        customerFacingJson={
            "customerFacingName": combination.get("customerFacingName"),
            "customerFacingDescription": combination.get("customerFacingDescription"),
            "customerFacingWhySuits": combination.get("customerFacingWhySuits"),
            "customerFacingBestUse": combination.get("customerFacingBestUse"),
            "customerFacingWeatherSuitability": combination.get("customerFacingWeatherSuitability"),
            "customerFacingStrength": combination.get("customerFacingStrength"),
            "customerFacingRisk": combination.get("customerFacingRisk"),
            "customerFacingNotesByProduct": combination.get("customerFacingNotesByProduct"),
            "components": combination.get("components"),
            "combinedDirection": combination.get("combinedDirection"),
            "sharedOrConnectingNotes": combination.get("sharedOrConnectingNotes"),
            "whyNotesWork": combination.get("whyNotesWork"),
            "expectedResult": combination.get("expectedResult"),
            "customerFacingHistoricalEvidence": combination.get("customerFacingHistoricalEvidence"),
            "existingCombinationEvidence": combination.get("existingCombinationEvidence"),
        },
        status="pending",
        createdAt=now,
    )
    session.add(record)
    await session.commit()
    return record.id


def to_customer_safe_recommendation(record: FragranceRecommendation) -> dict[str, Any]:
    """The only shape any customer-facing surface may ever read. Never includes productsJson/
    evidenceJson (real source titles/notes) or scoreJson's raw numbers.
    """
    score_json = record.scoreJson or {}
    return {
        "recommendationId": record.id,
        "type": record.combinationType,
        "existsAlready": False,
        "evidenceScope": record.evidenceScope,
        "confidence": score_json.get("confidence"),
        "confidenceBreakdown": score_json.get("confidenceBreakdown"),
        **(record.customerFacingJson or {}),
    }


async def get_recommendation(session: AsyncSession, recommendation_id: str) -> FragranceRecommendation | None:
    return await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == recommendation_id))


async def confirm_recommendation(
    session: AsyncSession, *, recommendation_id: str, customer_name: str | None, customer_email: str | None
) -> dict[str, Any]:
    """Re-verifies everything deterministically before Shopify product creation is allowed to
    proceed -- never trusts the generation-time snapshot for any of these checks.
    """
    record = await get_recommendation(session, recommendation_id)
    if not record:
        return {"ok": False, "reason": "Recommendation not found."}
    if record.status == "confirmed":
        return {"ok": False, "reason": "This recommendation has already been confirmed."}
    if record.status == "expired":
        return {"ok": False, "reason": "This recommendation has expired — please generate a new one."}

    if utcnow() - record.createdAt > RECOMMENDATION_EXPIRY:
        record.status = "expired"
        await session.commit()
        return {"ok": False, "reason": "This recommendation has expired — please generate a new one."}

    if not customer_name or not customer_email:
        return {"ok": False, "reason": "Customer name and email must be available from the Shopify account before creating a product."}

    products = record.productsJson if isinstance(record.productsJson, list) else []
    expected_count = COMPONENT_COUNT_BY_TYPE.get(record.combinationType)
    if not expected_count or len(products) != expected_count:
        return {"ok": False, "reason": f"Product count ({len(products)}) doesn't match {record.combinationType} (expects {expected_count})."}

    for p in products:
        catalog_product = await session.scalar(
            select(FragranceProduct).where(FragranceProduct.normalizedTitle == normalize_product_name(p["title"]))
        )
        if not catalog_product:
            return {"ok": False, "reason": f'Product "{p["title"]}" no longer exists in the catalog.'}
        if not isinstance(catalog_product.notesJson, list) or len(catalog_product.notesJson) == 0:
            return {"ok": False, "reason": f'Product "{p["title"]}" has no notes data.'}

    # Must not already exist as a real combination now -- this engine only ever proposes genuinely
    # new combinations, so re-confirm it's still new (the catalog could have changed since).
    component_key = (record.evidenceJson or {}).get("canonicalKey")
    if component_key:
        existing = await session.scalar(select(ExistingCombination).where(ExistingCombination.componentKey == component_key))
        if existing:
            return {"ok": False, "reason": f'"{existing.title}" already exists as a real combination now — cannot create a duplicate.'}

    ratios = record.ratiosJson if isinstance(record.ratiosJson, list) else []
    pct_sum = sum(r.get("ratioPercent") or 0 for r in ratios)
    if pct_sum != 100:
        return {"ok": False, "reason": f"Ratios sum to {pct_sum}%, not 100%."}

    # Recompute dislike-conflict severity against the CURRENT profile's dislikes -- never trusts
    # the generation-time snapshot for this safety check.
    split = split_dislikes_by_exactness((record.customerProfileJson or {}).get("dislikes") or [])
    for p in products:
        if literal_note_match_count(p.get("notes"), split["exactNoteDislikes"]) > 0:
            return {"ok": False, "reason": f'"{p["title"]}" contains a note the customer explicitly disliked — cannot confirm.'}
        conflict = classify_dislike_conflict(p.get("notes"), split["explicitFamilyDislikes"])
        if conflict["severity"] == "high":
            return {"ok": False, "reason": f'"{p["title"]}" has a high-severity conflict with a disliked note/family — cannot confirm.'}

    record.status = "confirmed"
    record.confirmedAt = utcnow()
    await session.commit()

    return {"ok": True, "recommendation": record}


async def mark_recommendation_shopify_product(session: AsyncSession, recommendation_id: str, shopify_product_id: str) -> None:
    record = await get_recommendation(session, recommendation_id)
    if record:
        record.shopifyProductId = shopify_product_id
        await session.commit()
