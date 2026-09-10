"""Phase 1 regression tests for N3: build capability tokens (app/services/build_capability.py).
Database-backed (real Postgres; the schema-only local instance is enough -- no catalog data is
needed)."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from app.db.models import BuildCapability, Conversation, FragranceRecommendation
from app.db.time import utcnow
from app.services.build_capability import (
    BuildNotAuthorized,
    authorize_build_token,
    hash_build_token,
    issue_build_token,
    preview_url_for_logging,
    revoke_build_tokens,
)

SHOP = "test-shop.myshopify.com"


async def _make_recommendation(session):
    conversation_id = f"pytest-cap-{uuid.uuid4().hex[:8]}"
    rec = FragranceRecommendation(
        id=f"pytest-cap-rec-{uuid.uuid4().hex[:8]}", conversationId=conversation_id, createdAt=utcnow(),
        customerProfileJson={}, productsJson=[{"title": "A", "notes": [], "contribution": "x"}], combinationType="HYBRID",
        scoreJson={}, evidenceJson={}, ratiosJson=[], customerFacingJson={}, status="confirmed", buildStatus="draft",
    )
    session.add(rec)
    session.add(Conversation(id=conversation_id, createdAt=utcnow(), updatedAt=utcnow()))
    await session.commit()
    return rec


async def _cleanup(session, *recs):
    for rec in recs:
        await session.execute(delete(FragranceRecommendation).where(FragranceRecommendation.id == rec.id))
        await session.execute(delete(Conversation).where(Conversation.id == rec.conversationId))
    await session.commit()


async def test_token_is_random_scoped_and_stored_only_as_a_hash(db_session):
    rec = await _make_recommendation(db_session)
    try:
        token_a = await issue_build_token(db_session, recommendation_id=rec.id, conversation_id=rec.conversationId, shop=SHOP)
        token_b = await issue_build_token(db_session, recommendation_id=rec.id, conversation_id=rec.conversationId, shop=SHOP)
        assert token_a != token_b and len(token_a) >= 40
        assert rec.id not in token_a and rec.conversationId not in token_a
        rows = (await db_session.execute(select(BuildCapability).where(BuildCapability.recommendationId == rec.id))).scalars().all()
        assert {r.tokenHash for r in rows} == {hash_build_token(token_a), hash_build_token(token_b)}
        assert all(token_a not in (r.tokenHash or "") and token_b not in (r.tokenHash or "") for r in rows)
        assert all(r.shop == SHOP and r.conversationId == rec.conversationId for r in rows)

        capability = await authorize_build_token(db_session, token=token_a, recommendation_id=rec.id)
        assert capability.recommendationId == rec.id
    finally:
        await _cleanup(db_session, rec)


async def test_wrong_missing_or_foreign_tokens_are_rejected(db_session):
    rec_a = await _make_recommendation(db_session)
    rec_b = await _make_recommendation(db_session)
    try:
        token_a = await issue_build_token(db_session, recommendation_id=rec_a.id, conversation_id=rec_a.conversationId, shop=SHOP)
        for token, rec_id in [
            (token_a, rec_b.id),                      # token for A used on B
            (token_a[:-1] + ("A" if token_a[-1] != "A" else "B"), rec_a.id),
            ("", rec_a.id), (None, rec_a.id), ("x" * 1000, rec_a.id),
            (hash_build_token(token_a), rec_a.id),    # presenting the stored hash is not the token
            (token_a, ""), (token_a, None), (token_a, "does-not-exist"),
        ]:
            with pytest.raises(BuildNotAuthorized):
                await authorize_build_token(db_session, token=token, recommendation_id=rec_id)
    finally:
        await _cleanup(db_session, rec_a, rec_b)


async def test_expired_and_revoked_tokens_are_rejected(db_session):
    rec = await _make_recommendation(db_session)
    try:
        expired = await issue_build_token(db_session, recommendation_id=rec.id, conversation_id=rec.conversationId, shop=SHOP)
        row = await db_session.scalar(select(BuildCapability).where(BuildCapability.tokenHash == hash_build_token(expired)))
        row.expiresAt = utcnow() - timedelta(seconds=1)
        await db_session.commit()
        with pytest.raises(BuildNotAuthorized):
            await authorize_build_token(db_session, token=expired, recommendation_id=rec.id)

        live = await issue_build_token(db_session, recommendation_id=rec.id, conversation_id=rec.conversationId, shop=SHOP)
        await authorize_build_token(db_session, token=live, recommendation_id=rec.id)
        assert await revoke_build_tokens(db_session, rec.id) >= 1
        with pytest.raises(BuildNotAuthorized):
            await authorize_build_token(db_session, token=live, recommendation_id=rec.id)
    finally:
        await _cleanup(db_session, rec)


async def test_deleting_the_recommendation_cascades_its_capabilities(db_session):
    rec = await _make_recommendation(db_session)
    token = await issue_build_token(db_session, recommendation_id=rec.id, conversation_id=rec.conversationId, shop=SHOP)
    await _cleanup(db_session, rec)
    assert await db_session.scalar(select(BuildCapability).where(BuildCapability.tokenHash == hash_build_token(token))) is None


def test_preview_url_logging_strips_the_token():
    url = "https://test-shop.myshopify.com/apps/scent-library/fragrance-preview?recommendationId=abc&bt=SECRET-TOKEN"
    assert preview_url_for_logging(url) == "https://test-shop.myshopify.com/apps/scent-library/fragrance-preview?recommendationId=abc"
    assert preview_url_for_logging(None) is None
