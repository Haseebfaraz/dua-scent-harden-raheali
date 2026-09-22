import time
import uuid

import pytest
from sqlalchemy import delete, select

from app.db.ids import new_id
from app.db.models import FragranceProduct, FragranceRecommendation
from app.db.time import utcnow
from app.services.recommendation_confirmation import (
    confirm_recommendation,
    get_recommendation,
    mark_recommendation_draft,
    mark_recommendation_saved,
    save_recommendation,
)


pytestmark = pytest.mark.usefixtures("synthetic_catalog")

async def _base_combination(session):
    real_product = await session.scalar(
        select(FragranceProduct).where(FragranceProduct.notesJson.is_not(None)).where(~FragranceProduct.title.contains("pytest")).limit(1)
    )
    real_product_2 = await session.scalar(
        select(FragranceProduct)
        .where(FragranceProduct.notesJson.is_not(None))
        .where(FragranceProduct.title != real_product.title)
        .where(~FragranceProduct.title.contains("pytest"))
        .limit(1)
    )
    return {
        "type": "HYBRID",
        "canonicalKey": f"pytest-fake-key-{time.time()}-{uuid.uuid4().hex[:8]}",
        "internalProducts": [
            {"title": real_product.title, "notes": real_product.notesJson, "fragranceFamily": None, "contribution": "Freshness"},
            {"title": real_product_2.title, "notes": real_product_2.notesJson, "fragranceFamily": None, "contribution": "Sweetness"},
        ],
        "recommendedRatio": [
            {"productTitle": real_product.title, "parts": 1, "ratioPercent": 50, "milliliters": 17},
            {"productTitle": real_product_2.title, "parts": 1, "ratioPercent": 50, "milliliters": 17},
        ],
        "preferenceScore": 5, "seasonalScore": 4, "historyScore": 5, "compatibilityScore": 5, "balanceScore": 10,
        "conflictPenalty": 0, "finalScore": 29, "confidence": "high", "evidenceScope": "limited",
        "compatibilityReasons": [], "historicalEvidence": {}, "analogousExistingCombinations": [], "risks": [],
        "customerFacingName": "Test Blend", "customerFacingDescription": "test", "customerFacingWhySuits": "test",
        "customerFacingBestUse": "test", "customerFacingWeatherSuitability": "test", "customerFacingStrength": "moderate",
        "customerFacingRisk": None,
    }


async def _save_test_recommendation(session, overrides=None):
    combination = {**(await _base_combination(session)), **(overrides or {})}
    conversation_id = f"pytest-confirm-{time.time()}-{uuid.uuid4().hex[:8]}"
    recommendation_id = await save_recommendation(session, conversation_id=conversation_id, profile={"dislikes": []}, combination=combination)
    return recommendation_id, combination, conversation_id


async def _cleanup(session, *ids):
    await session.execute(delete(FragranceRecommendation).where(FragranceRecommendation.id.in_(ids)))
    await session.commit()


@pytest.mark.asyncio
async def test_mark_recommendation_draft_sets_build_status_name_and_ratios(db_session):
    recommendation_id, _, _ = await _save_test_recommendation(db_session)
    try:
        record = await mark_recommendation_draft(db_session, recommendation_id, name="My Draft Name", ratios={"top": 40, "middle": 30, "base": 30})
        assert record.buildStatus == "draft"
        assert record.draftName == "My Draft Name"
        assert record.draftRatiosJson == {"top": 40, "middle": 30, "base": 30}

        reloaded = await get_recommendation(db_session, recommendation_id)
        assert reloaded.draftName == "My Draft Name"
    finally:
        await _cleanup(db_session, recommendation_id)


@pytest.mark.asyncio
async def test_mark_recommendation_draft_leaves_name_and_ratios_untouched_when_none(db_session):
    recommendation_id, _, _ = await _save_test_recommendation(db_session)
    try:
        await mark_recommendation_draft(db_session, recommendation_id, name="First Name", ratios={"top": 34, "middle": 33, "base": 33})
        record = await mark_recommendation_draft(db_session, recommendation_id, name=None, ratios=None)
        assert record.draftName == "First Name"
        assert record.draftRatiosJson == {"top": 34, "middle": 33, "base": 33}
    finally:
        await _cleanup(db_session, recommendation_id)


@pytest.mark.asyncio
async def test_mark_recommendation_draft_returns_none_for_unknown_id():
    from app.db.session import SessionLocal

    async with SessionLocal() as session:
        assert await mark_recommendation_draft(session, "does-not-exist", name="x", ratios=None) is None


@pytest.mark.asyncio
async def test_mark_recommendation_saved_sets_build_status_and_shopify_ids(db_session):
    recommendation_id, _, _ = await _save_test_recommendation(db_session)
    try:
        record = await mark_recommendation_saved(
            db_session, recommendation_id, shopify_product_id="gid://shopify/Product/1", shopify_variant_id="gid://shopify/ProductVariant/1"
        )
        assert record.buildStatus == "saved"
        assert record.shopifyProductId == "gid://shopify/Product/1"
        assert record.shopifyVariantId == "gid://shopify/ProductVariant/1"
    finally:
        await _cleanup(db_session, recommendation_id)


@pytest.mark.asyncio
async def test_confirm_succeeds_for_fresh_valid_recommendation(db_session):
    recommendation_id, _, _ = await _save_test_recommendation(db_session)
    try:
        result = await confirm_recommendation(db_session, recommendation_id=recommendation_id, customer_name="Test Customer", customer_email="test@example.com")
        assert result["ok"] is True
        assert result["recommendation"].status == "confirmed"
    finally:
        await _cleanup(db_session, recommendation_id)


@pytest.mark.asyncio
async def test_confirm_rejects_nonexistent_recommendation(db_session):
    result = await confirm_recommendation(db_session, recommendation_id="nonexistent-id", customer_name="X", customer_email="x@example.com")
    assert result == {"ok": False, "reasonCode": "not_found", "reason": "Recommendation not found."}


@pytest.mark.asyncio
async def test_confirm_rejects_double_confirm(db_session):
    recommendation_id, _, _ = await _save_test_recommendation(db_session)
    try:
        await confirm_recommendation(db_session, recommendation_id=recommendation_id, customer_name="Test Customer", customer_email="test@example.com")
        second = await confirm_recommendation(db_session, recommendation_id=recommendation_id, customer_name="Test Customer", customer_email="test@example.com")
        assert second["ok"] is False
        assert "already been confirmed" in second["reason"]
    finally:
        await _cleanup(db_session, recommendation_id)


@pytest.mark.asyncio
async def test_confirm_rejects_missing_customer_info(db_session):
    recommendation_id, _, _ = await _save_test_recommendation(db_session)
    try:
        result = await confirm_recommendation(db_session, recommendation_id=recommendation_id, customer_name=None, customer_email=None)
        assert result["ok"] is False
        assert result["reasonCode"] == "identity_missing"
        assert "Customer name and email" in result["reason"]
    finally:
        await _cleanup(db_session, recommendation_id)


@pytest.mark.asyncio
async def test_save_recommendation_rejects_bad_ratio_sum(db_session):
    combination = await _base_combination(db_session)
    combination["recommendedRatio"] = [{**r, "ratioPercent": 40} for r in combination["recommendedRatio"]]
    with pytest.raises(ValueError, match="sum to 100"):
        await save_recommendation(
            db_session, conversation_id=f"pytest-confirm-badratio-{time.time()}", profile={"dislikes": []}, combination=combination,
        )


@pytest.mark.asyncio
async def test_confirm_independently_rechecks_ratios(db_session):
    combination = await _base_combination(db_session)
    now = utcnow()
    record = FragranceRecommendation(
        id=new_id(), conversationId=f"pytest-confirm-badratio-direct-{time.time()}",
        customerProfileJson={"dislikes": []}, productsJson=combination["internalProducts"],
        combinationType=combination["type"], scoreJson={}, evidenceJson={"canonicalKey": combination["canonicalKey"]},
        ratiosJson=[{**r, "ratioPercent": 40} for r in combination["recommendedRatio"]],
        status="pending", createdAt=now,
    )
    db_session.add(record)
    await db_session.commit()
    try:
        result = await confirm_recommendation(db_session, recommendation_id=record.id, customer_name="Test Customer", customer_email="test@example.com")
        assert result["ok"] is False
        assert "Ratios sum to" in result["reason"]
    finally:
        await _cleanup(db_session, record.id)


@pytest.mark.asyncio
async def test_confirm_rejects_expired_and_marks_expired(db_session):
    combination = await _base_combination(db_session)
    from datetime import timedelta

    record = FragranceRecommendation(
        id=new_id(), conversationId=f"pytest-confirm-expired-{time.time()}",
        customerProfileJson={"dislikes": []}, productsJson=combination["internalProducts"],
        combinationType=combination["type"], scoreJson={}, evidenceJson={"canonicalKey": combination["canonicalKey"]},
        ratiosJson=combination["recommendedRatio"], status="pending", createdAt=utcnow() - timedelta(hours=48),
    )
    db_session.add(record)
    await db_session.commit()
    try:
        result = await confirm_recommendation(db_session, recommendation_id=record.id, customer_name="Test Customer", customer_email="test@example.com")
        assert result["ok"] is False
        assert "expired" in result["reason"]

        reloaded = await get_recommendation(db_session, record.id)
        assert reloaded.status == "expired"
    finally:
        await _cleanup(db_session, record.id)


@pytest.mark.asyncio
async def test_confirm_rejects_high_severity_dislike_conflict(db_session):
    combination = await _base_combination(db_session)
    combination["internalProducts"][0]["notes"] = ["Oud", "Leather", "Tobacco", "Resin"]
    conversation_id = f"pytest-confirm-conflict-{time.time()}"
    recommendation_id = await save_recommendation(db_session, conversation_id=conversation_id, profile={"dislikes": ["Strong"]}, combination=combination)
    try:
        result = await confirm_recommendation(db_session, recommendation_id=recommendation_id, customer_name="Test Customer", customer_email="test@example.com")
        assert result["ok"] is False
        assert "high-severity conflict" in result["reason"]
    finally:
        await _cleanup(db_session, recommendation_id)


@pytest.mark.asyncio
async def test_dedup_reuses_existing_recommendation_same_conversation(db_session):
    conversation_id = f"pytest-dedup-{time.time()}-{uuid.uuid4().hex[:8]}"
    combination = await _base_combination(db_session)
    first_id = await save_recommendation(db_session, conversation_id=conversation_id, profile={"dislikes": []}, combination=combination)
    try:
        second_id = await save_recommendation(db_session, conversation_id=conversation_id, profile={"dislikes": []}, combination=combination)
        assert second_id == first_id

        rows = (await db_session.execute(select(FragranceRecommendation.evidenceJson).where(FragranceRecommendation.conversationId == conversation_id))).all()
        count = sum(1 for (evidence,) in rows if (evidence or {}).get("canonicalKey") == combination["canonicalKey"])
        assert count == 1
    finally:
        await _cleanup(db_session, first_id)


@pytest.mark.asyncio
async def test_dedup_does_not_cross_conversations(db_session):
    combination = await _base_combination(db_session)
    first_id = await save_recommendation(db_session, conversation_id=f"pytest-dedup-a-{time.time()}", profile={"dislikes": []}, combination=combination)
    second_id = await save_recommendation(db_session, conversation_id=f"pytest-dedup-b-{time.time()}", profile={"dislikes": []}, combination=combination)
    try:
        assert second_id != first_id
    finally:
        await _cleanup(db_session, first_id, second_id)


@pytest.mark.asyncio
async def test_dedup_does_not_match_expired_recommendation(db_session):
    from datetime import timedelta

    combination = await _base_combination(db_session)
    conversation_id = f"pytest-dedup-expired-{time.time()}"
    old = FragranceRecommendation(
        id=new_id(), conversationId=conversation_id, customerProfileJson={"dislikes": []},
        productsJson=combination["internalProducts"], combinationType=combination["type"], scoreJson={},
        evidenceJson={"canonicalKey": combination["canonicalKey"]}, ratiosJson=combination["recommendedRatio"],
        status="pending", createdAt=utcnow() - timedelta(hours=48),
    )
    db_session.add(old)
    await db_session.commit()
    fresh_id = await save_recommendation(db_session, conversation_id=conversation_id, profile={"dislikes": []}, combination=combination)
    try:
        assert fresh_id != old.id
    finally:
        await _cleanup(db_session, old.id, fresh_id)
