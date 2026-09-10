"""Phase 1 regression tests: every code path that emits preview_ready mints a build capability,
persists only its hash, and puts the plaintext token (and nothing else secret) into the preview
URL; log lines never contain the token. Database-backed (schema-only), catalog data synthetic."""

import json
import logging
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import delete, select

from app.ai import tool_executor
from app.ai.tool_executor import execute_fragrance_tool
from app.db.ids import new_id
from app.db.models import BuildCapability, Conversation, CustomerProfileState, FragranceProduct, FragranceRecommendation
from app.db.time import utcnow
from app.services import legacy_preview_recovery
from app.services.build_capability import hash_build_token
from app.services.customer_profile import save_customer_profile_fields
from app.services.recommendation_confirmation import save_recommendation

SHOP = "test-shop.myshopify.com"


def _synthetic_candidate(title_a, title_b):
    return {
        "type": "HYBRID", "canonicalKey": f"pytest-cap-{uuid.uuid4().hex[:8]}",
        "internalProducts": [
            {"title": title_a, "notes": ["Bergamot", "Lemon"], "fragranceFamily": None, "contribution": "Freshness"},
            {"title": title_b, "notes": ["Vanilla", "Musk"], "fragranceFamily": None, "contribution": "Sweetness"},
        ],
        "recommendedRatio": [{"productTitle": title_a, "ratioPercent": 50}, {"productTitle": title_b, "ratioPercent": 50}],
        "confidenceBreakdown": {"customerFit": {"value": "high"}, "compatibility": {"value": "high"}},
        "riskBreakdown": [], "requestedPreferenceFamilies": ["fresh"], "matchedPreferenceFamilies": ["fresh"],
        "customerFacingName": "Pytest Blend", "customerFacingWhySuits": "x", "customerFacingBestUse": "x",
        "customerFacingStrength": "moderate", "customerFacingWeatherSuitability": "x", "customerFacingRisk": None,
    }


@pytest.fixture
async def synthetic_products(db_session):
    titles = [f"Pytest Cap Product A {uuid.uuid4().hex[:6]}", f"Pytest Cap Product B {uuid.uuid4().hex[:6]}"]
    from app.fragrance.normalization import normalize_product_name

    rows = [FragranceProduct(id=new_id(), title=t, normalizedTitle=normalize_product_name(t), notesJson=["Bergamot", "Lemon"] if i == 0 else ["Vanilla", "Musk"], createdAt=utcnow(), updatedAt=utcnow()) for i, t in enumerate(titles)]
    db_session.add_all(rows)
    await db_session.commit()
    yield titles
    for r in rows:
        await db_session.delete(r)
    await db_session.commit()


async def _cleanup(session, conversation_id):
    await session.execute(delete(FragranceRecommendation).where(FragranceRecommendation.conversationId == conversation_id))
    await session.execute(delete(CustomerProfileState).where(CustomerProfileState.conversationId == conversation_id))
    await session.execute(delete(Conversation).where(Conversation.id == conversation_id))
    await session.commit()


async def test_generate_flow_mints_a_capability_and_never_logs_it(db_session, synthetic_products, monkeypatch, caplog):
    conversation_id = f"pytest-cap-flow-{uuid.uuid4().hex[:8]}"
    title_a, title_b = synthetic_products
    candidate = _synthetic_candidate(title_a, title_b)

    async def _analyze(session, profile):
        return [{"productName": title_a, "normalizedProductName": title_a.lower(), "relevanceScore": 1}]

    async def _generate(session, **kw):
        return [dict(candidate)]

    async def _inventory(session, c, idx=None):
        return {"buildable": True, "inventoryValidated": False, "components": [], "requestCount": 0, "skusQueried": [], "durationMs": 0, "status": "ok", "oilTotalMl": 13, "alcoholMl": 21, "maxBuildableBottles": None, "limitingSku": None}

    monkeypatch.setattr(tool_executor, "analyze_customer_product_candidates", _analyze)
    monkeypatch.setattr(tool_executor, "generate_new_product_combinations", _generate)
    monkeypatch.setattr(tool_executor, "evaluate_candidate_inventory", _inventory)
    try:
        await save_customer_profile_fields(db_session, conversation_id, {"likes": ["Fresh"], "dislikesAsked": True, "occasionAsked": True, "strengthPreference": "moderate", "locationAsked": True, "name": "Pytest"})
        ctx = {"conversationId": conversation_id, "customerName": "Pytest", "customerEmail": "pytest@example.test", "shopDomain": SHOP}
        with caplog.at_level(logging.INFO):
            result = await execute_fragrance_tool(db_session, "generate_new_product_combinations", "{}", ctx)

        assert result["sseEvent"]["type"] == "preview_ready", result["modelContent"]
        url = result["sseEvent"]["previewUrl"]
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        assert parts.netloc == SHOP
        token = query["bt"][0]
        rec_id = query["recommendationId"][0]
        assert len(token) >= 40

        row = await db_session.scalar(select(BuildCapability).where(BuildCapability.recommendationId == rec_id))
        assert row is not None and row.tokenHash == hash_build_token(token) and row.conversationId == conversation_id and row.shop == SHOP
        # The plaintext token is not in the recommendation id, the model-facing text, or any log line.
        assert token not in rec_id and token not in result["modelContent"]
        assert all(token not in r.getMessage() for r in caplog.records)
        assert any("PREVIEW_READY_EMITTED" in r.getMessage() and "recommendationId=" in json.loads(r.getMessage().split(" ", 1)[1])["previewUrl"] for r in caplog.records)
    finally:
        await _cleanup(db_session, conversation_id)


async def test_legacy_preview_recovery_mints_a_capability(db_session, synthetic_products):
    conversation_id = f"pytest-cap-legacy-{uuid.uuid4().hex[:8]}"
    title_a, title_b = synthetic_products
    try:
        rec_id = await save_recommendation(db_session, conversation_id=conversation_id, profile={"dislikes": []}, combination=_synthetic_candidate(title_a, title_b))
        result = await legacy_preview_recovery.resolve_legacy_preview_short_circuit(db_session, conversation_id, "preview", "Pytest", "pytest@example.test", SHOP)
        assert result and result["recommendationId"] == rec_id
        token = parse_qs(urlsplit(result["previewUrl"]).query)["bt"][0]
        row = await db_session.scalar(select(BuildCapability).where(BuildCapability.tokenHash == hash_build_token(token)))
        assert row is not None and row.recommendationId == rec_id
    finally:
        await _cleanup(db_session, conversation_id)


async def test_confirm_product_combination_mints_a_capability(db_session, synthetic_products):
    conversation_id = f"pytest-cap-confirm-{uuid.uuid4().hex[:8]}"
    title_a, title_b = synthetic_products
    try:
        rec_id = await save_recommendation(db_session, conversation_id=conversation_id, profile={"dislikes": []}, combination=_synthetic_candidate(title_a, title_b))
        ctx = {"conversationId": conversation_id, "customerName": "Pytest", "customerEmail": "pytest@example.test", "shopDomain": SHOP}
        result = await execute_fragrance_tool(db_session, "confirm_product_combination", json.dumps({"recommendationId": rec_id}), ctx)
        assert result["sseEvent"]["type"] == "preview_ready", result["modelContent"]
        token = parse_qs(urlsplit(result["sseEvent"]["previewUrl"]).query)["bt"][0]
        assert await db_session.scalar(select(BuildCapability).where(BuildCapability.tokenHash == hash_build_token(token))) is not None
    finally:
        await _cleanup(db_session, conversation_id)
