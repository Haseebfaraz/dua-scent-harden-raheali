"""Phase 6 (F11, N14, logging) regressions: ownership-aware deletion, minimization of retained
commerce records, resurrection protection, bounded dry-run-first retention, a read-only history
route, and privacy-safe logs and errors.

Every record here is synthetic (markers below), lives in the disposable local PostgreSQL, and is
removed afterwards. No Shopify, Odoo or model call is made; the suite-wide network guard would
fail any test that tried.
"""

import asyncio
import io
import json
import logging
import threading
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text

from app.ai import conversation_flow, tool_executor
from app.ai.openai_client import call_openai_once as REAL_CALL_OPENAI_ONCE  # captured before any fixture patches it
from app.api import chat as chat_module
from app.config import settings
from app.db.ids import new_id
from app.db.models import (
    BuildCapability, Conversation, ConversationCapability, ConversationDeletion, CustomerAccountUrls, CustomerProfileState,
    FragranceRecommendation, Message, MessageSecurityClassification, RateLimitBucket, RecommendationInventorySnapshot,
)
from app.db.session import SessionLocal
from app.db.time import utcnow
from app.logging_config import JsonFormatter, safe_exception_summary
from app.main import app
from app.services import data_lifecycle
from app.services.build_capability import issue_build_token
from app.services.build_commerce import build_commerce_lock
from app.services.conversation import create_or_update_conversation, save_message, save_user_message_with_classification
from app.services.conversation_capability import CONVERSATION_TOKEN_HEADER, ConversationNotAuthorized, authorize_conversation, create_conversation_with_capability
from app.services.customer_profile import get_customer_profile, save_customer_profile_fields
from app.services.data_lifecycle import (
    DETACHED_PREFIX, ConversationDeleted, conversation_key, delete_conversation, run_retention,
)
from app.services.inventory_snapshot import save_inventory_snapshot
from app.services.recommendation_confirmation import mark_recommendation_draft, save_recommendation
from app.services.turn_lock import conversation_turn_lock

# Synthetic personal-data markers. If any of these survives where it should not, a test fails.
NAME = "Zedekiah Markerperson"
EMAIL = "pii.marker.7731@example.test"
CITY = "Markerville"
MESSAGE = "my sister Quillona Markerperson wears rose, call me on 555-0199-7731"
BLEND_NAME = "Blend For Zedekiah M"
FREE_TEXT = "remember my anniversary with Quillona on the 4th"
MARKERS = (NAME, EMAIL, CITY, "Quillona", "555-0199-7731", BLEND_NAME, "Zedekiah", "anniversary")
PRIVATE_TITLE = "PRIVATE-SOURCE-TITLE-MARKER-88"


class World:
    """Tracks everything a test creates so it can be removed even when deletion is refused."""

    def __init__(self):
        self.conversation_ids: list[str] = []
        self.recommendation_ids: list[str] = []

    async def conversation(self, *, age_days: float = 0, last_customer_message_days: float | None = None, with_data: bool = True) -> dict:
        async with SessionLocal() as session:
            conversation_id, token, _ = await create_conversation_with_capability(session)
            self.conversation_ids.append(conversation_id)
            created = utcnow() - timedelta(days=age_days)
            conversation = await session.scalar(select(Conversation).where(Conversation.id == conversation_id))
            conversation.customerName, conversation.customerEmail, conversation.createdAt = NAME, EMAIL, created
            await session.commit()
            if with_data:
                stamp = utcnow() - timedelta(days=age_days if last_customer_message_days is None else last_customer_message_days)
                message = Message(id=new_id(), conversationId=conversation_id, role="user", content=MESSAGE, createdAt=stamp)
                session.add(message)
                session.add(Message(id=new_id(), conversationId=conversation_id, role="assistant", content=f"Lovely, {NAME}.", createdAt=stamp))
                session.add(MessageSecurityClassification(id=new_id(), messageId=message.id, classification="FRAGRANCE", reasonCode="NONE", classifierVersion="t", createdAt=stamp))
                session.add(CustomerAccountUrls(id=new_id(), conversationId=conversation_id, mcpApiUrl="https://synthetic.invalid/mcp", createdAt=stamp, updatedAt=stamp))
                session.add(RateLimitBucket(key=f"chat_turn_conv:{conversation_id}", windowStart=stamp, count=3, updatedAt=utcnow()))
                await session.commit()
                await save_customer_profile_fields(session, conversation_id, {"name": NAME, "email": EMAIL, "city": CITY, "likes": ["Rose"], "additionalPreferences": [FREE_TEXT]})
        return {"conversationId": conversation_id, "conversationToken": token}

    async def recommendation(self, conversation_id: str, *, build_status: str = "draft", product_id: str | None = None, age_days: float = 0) -> str:
        recommendation_id = f"pytest-f11-rec-{uuid.uuid4().hex[:10]}"
        async with SessionLocal() as session:
            session.add(FragranceRecommendation(
                id=recommendation_id, conversationId=conversation_id, customerProfileJson={"name": NAME, "email": EMAIL, "likes": ["Rose"], "city": CITY},
                productsJson=[{"title": PRIVATE_TITLE, "notes": ["Rose"], "contribution": f"chosen for {NAME}", "handle": "private-handle"}], combinationType="HYBRID",
                scoreJson={"total": 0.91}, evidenceJson={"cohort": f"customers like {NAME}"}, ratiosJson=[{"productTitle": PRIVATE_TITLE, "ratioPercent": 100}], evidenceScope="regional",
                customerFacingJson={"customerFacingName": BLEND_NAME, "whySuits": f"For {NAME} and Quillona"}, status="confirmed", createdAt=utcnow() - timedelta(days=age_days), confirmedAt=utcnow(),
                shopifyProductId=product_id, shopifyVariantId="gid://shopify/ProductVariant/1" if product_id else None, buildStatus=build_status,
                draftName=BLEND_NAME, draftRatiosJson={"top": 34, "middle": 33, "base": 33}, draftExcludedNotes=["Quillona's least favourite"],
            ))
            await session.commit()
            await save_inventory_snapshot(session, recommendation_id=recommendation_id, inventory_validated=False, buildable=True, checked_at=utcnow(), oil_total_ml=13, alcohol_ml=21,
                                          request_status="ok", max_buildable_bottles=None, limiting_sku=None, components=[])
            await issue_build_token(session, recommendation_id=recommendation_id, conversation_id=conversation_id, shop="test-shop.myshopify.com")
        self.recommendation_ids.append(recommendation_id)
        return recommendation_id

    async def cleanup(self) -> None:
        async with SessionLocal() as session:
            for rid in self.recommendation_ids:
                await session.execute(delete(BuildCapability).where(BuildCapability.recommendationId == rid))
                await session.execute(delete(FragranceRecommendation).where(FragranceRecommendation.id == rid))
            for cid in self.conversation_ids:
                await session.execute(delete(FragranceRecommendation).where(FragranceRecommendation.conversationId == cid))
                await session.execute(delete(CustomerProfileState).where(CustomerProfileState.conversationId == cid))
                await session.execute(delete(CustomerAccountUrls).where(CustomerAccountUrls.conversationId == cid))
                await session.execute(delete(Conversation).where(Conversation.id == cid))
                await session.execute(delete(ConversationDeletion).where(ConversationDeletion.conversationKey == conversation_key(cid)))
                await session.execute(text('DELETE FROM "RateLimitBucket" WHERE "key" LIKE :p').bindparams(p=f"%{cid}%"))
            await session.commit()


@pytest.fixture
async def world():
    w = World()
    conversation_flow._CONVERSATIONS.clear()
    yield w
    await w.cleanup()
    conversation_flow._CONVERSATIONS.clear()


@pytest.fixture(autouse=True)
def _deletion_enabled(monkeypatch):
    monkeypatch.setattr(settings, "shared_data_deletion_reviewed", True)


@pytest.fixture(autouse=True)
def _model(monkeypatch):
    async def _fake(messages, tools, tool_choice=None):
        return {"choices": [{"finish_reason": "stop", "message": {"content": None if tool_choice else "Tell me more about the scent."}}]}

    monkeypatch.setattr(conversation_flow, "call_openai_once", _fake)
    monkeypatch.setattr("app.ai.openai_client.call_openai_once", _fake)


def _delete(client, conversation: dict, token: str | None = "own", **extra):
    headers = {} if token is None else {CONVERSATION_TOKEN_HEADER: conversation["conversationToken"] if token == "own" else token}
    return client.post("/chat/delete", json={"conversation_id": conversation["conversationId"], **extra}, headers=headers)


async def _counts(conversation_id: str) -> dict[str, int]:
    async with SessionLocal() as session:
        async def n(model, column):
            return await session.scalar(select(func.count()).select_from(model).where(column == conversation_id))
        return {
            "conversation": await n(Conversation, Conversation.id), "messages": await n(Message, Message.conversationId),
            "profile": await n(CustomerProfileState, CustomerProfileState.conversationId),
            "capabilities": await n(ConversationCapability, ConversationCapability.conversationId), "buildCapabilities": await n(BuildCapability, BuildCapability.conversationId),
            "recommendations": await n(FragranceRecommendation, FragranceRecommendation.conversationId),
            "buckets": await session.scalar(select(func.count()).select_from(RateLimitBucket).where(RateLimitBucket.key.like(f"%{conversation_id}"))),
        }


async def _database_dump() -> str:
    """Every text value in the tables that can hold customer data, for marker searches."""
    async with SessionLocal() as session:
        parts = []
        for table in ("Conversation", "Message", "CustomerProfileState", "FragranceRecommendation", "ConversationDeletion"):
            parts.append(json.dumps([dict(r._mapping) for r in (await session.execute(text(f'SELECT * FROM "{table}"'))).all()], default=str))
        return "\n".join(parts)


# ===========================================================================
# 1. Ownership
# ===========================================================================

async def test_owner_can_delete_their_conversation_and_everything_keyed_to_it(world):
    mine = await world.conversation()
    plain = await world.recommendation(mine["conversationId"])
    with TestClient(app) as client:
        response = _delete(client, mine)
    assert response.status_code == 200 and response.json()["status"] == "deleted" and response.json()["commerceRecordRetained"] is False
    assert "no-store" in response.headers.get("cache-control", "")
    assert set((await _counts(mine["conversationId"])).values()) == {0}
    async with SessionLocal() as session:
        # Phase 6A: the account-URL row (other application's, no personal data) is left alone.
        assert await session.scalar(select(func.count()).select_from(CustomerAccountUrls).where(CustomerAccountUrls.conversationId == mine["conversationId"])) == 1
        assert await session.scalar(select(func.count()).select_from(FragranceRecommendation).where(FragranceRecommendation.id == plain)) == 0
        assert await session.scalar(select(func.count()).select_from(RecommendationInventorySnapshot).where(RecommendationInventorySnapshot.recommendationId == plain)) == 0
        assert await session.scalar(select(func.count()).select_from(MessageSecurityClassification)
                                    .join(Message, Message.id == MessageSecurityClassification.messageId).where(Message.conversationId == mine["conversationId"])) == 0
    dump = await _database_dump()
    for marker in MARKERS:
        assert marker not in dump, marker


@pytest.mark.parametrize("attack", ["no_token", "garbage_token", "other_customers_token", "id_only_in_body", "victim_identity_in_body", "token_in_query"])
async def test_nothing_but_the_conversations_own_capability_authorizes_deletion(world, attack):
    victim = await world.conversation()
    attacker = await world.conversation()
    before = await _counts(victim["conversationId"])
    with TestClient(app) as client:
        if attack == "no_token":
            response = _delete(client, victim, token=None)
        elif attack == "garbage_token":
            response = _delete(client, victim, token="not-a-real-token-" + "x" * 30)
        elif attack == "other_customers_token":
            response = _delete(client, victim, token=attacker["conversationToken"])
        elif attack == "id_only_in_body":
            response = client.post("/chat/delete", json={"conversation_id": victim["conversationId"], "conversation_token": "", "confirm": True})
        elif attack == "victim_identity_in_body":
            response = _delete(client, victim, token=None, customer_email=EMAIL, customer_name=NAME, email=EMAIL, shop_domain="test-shop.myshopify.com")
        else:
            response = client.post(f"/chat/delete?conversation_token={victim['conversationToken']}&token={victim['conversationToken']}", json={"conversation_id": victim["conversationId"]})
        unknown = client.post("/chat/delete", json={"conversation_id": "0" * 32}, headers={CONVERSATION_TOKEN_HEADER: attacker["conversationToken"]})
    assert response.status_code == 401 and response.json() == unknown.json()  # identical to a conversation that does not exist
    assert await _counts(victim["conversationId"]) == before
    assert await _counts(attacker["conversationId"]) == await _counts(attacker["conversationId"])
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(ConversationDeletion).where(ConversationDeletion.conversationKey == conversation_key(victim["conversationId"]))) == 0


async def test_deleting_one_conversation_leaves_another_with_the_same_email_untouched(world):
    mine = await world.conversation()
    same_email_other_person = await world.conversation()  # identical name + email on purpose
    other_recommendation = await world.recommendation(same_email_other_person["conversationId"])
    before = await _counts(same_email_other_person["conversationId"])
    with TestClient(app) as client:
        assert _delete(client, mine).status_code == 200
    assert await _counts(same_email_other_person["conversationId"]) == before  # no deletion by email, ever
    async with SessionLocal() as session:
        kept = await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == other_recommendation))
        assert kept.customerFacingJson["customerFacingName"] == BLEND_NAME


async def test_signed_customer_identity_still_requires_object_ownership():
    async with SessionLocal() as session:
        conversation_id, token, _ = await create_conversation_with_capability(session, verified_shopify_customer_id="1001")
        other_id, _other_token, _ = await create_conversation_with_capability(session, verified_shopify_customer_id="1001")
        try:
            # The same verified customer id on another conversation is NOT ownership of this one.
            with pytest.raises(ConversationNotAuthorized):
                await authorize_conversation(session, token=token, conversation_id=other_id, verified_shopify_customer_id="1001")
            with pytest.raises(ConversationNotAuthorized):
                await authorize_conversation(session, token=token, conversation_id=conversation_id, verified_shopify_customer_id="2002")
            assert (await authorize_conversation(session, token=token, conversation_id=conversation_id, verified_shopify_customer_id="1001")).conversationId == conversation_id
        finally:
            for cid in (conversation_id, other_id):
                await session.execute(delete(Conversation).where(Conversation.id == cid))
            await session.commit()


def test_there_is_no_bulk_or_email_based_deletion_entry_point():
    import inspect

    paths = set(app.openapi()["paths"])
    assert "/chat/delete" in paths and not [p for p in paths if "delete" in p.lower() and p != "/chat/delete"]
    signature = inspect.signature(delete_conversation)
    assert set(signature.parameters) == {"session", "conversation_id", "origin"}
    source = inspect.getsource(data_lifecycle)
    assert "customerEmail" not in source and "WHERE email" not in source


# ===========================================================================
# 2. Removal and minimization
# ===========================================================================

async def test_commerce_records_are_minimized_to_an_allowlist_not_deleted_and_not_exposed(world):
    mine = await world.conversation()
    cid = mine["conversationId"]
    saved = await world.recommendation(cid, build_status="saved", product_id="gid://shopify/Product/777")
    pending = await world.recommendation(cid, build_status="pending_review")
    plain = await world.recommendation(cid)
    with TestClient(app) as client:
        body = _delete(client, mine).json()
    assert body["status"] == "deleted" and body["commerceRecordRetained"] is True
    assert "recommendation" not in json.dumps(body).lower() and saved not in json.dumps(body) and "777" not in json.dumps(body)  # nothing about the retained record

    async with SessionLocal() as session:
        assert await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == plain)) is None
        for rid, status in ((saved, "saved"), (pending, "pending_review")):
            row = await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == rid))
            assert row is not None and row.buildStatus == status  # the marker that prevents a duplicate creation survives
            assert row.conversationId.startswith(DETACHED_PREFIX) and cid not in row.conversationId
            assert row.customerProfileJson == {} and row.scoreJson == {} and row.evidenceJson == {} and row.evidenceScope is None
            assert row.customerFacingJson is None and row.draftName is None and row.draftExcludedNotes is None and row.draftRatiosJson is None
            assert row.productsJson == [{"title": PRIVATE_TITLE, "notes": ["Rose"]}]  # recipe keys only
            assert row.ratiosJson == [{"productTitle": PRIVATE_TITLE, "ratioPercent": 100}]
            assert await session.scalar(select(func.count()).select_from(BuildCapability).where(BuildCapability.recommendationId == rid)) == 0  # no capability can reach it
        assert (await session.scalar(select(FragranceRecommendation.shopifyProductId).where(FragranceRecommendation.id == saved))) == "gid://shopify/Product/777"
    dump = await _database_dump()
    for marker in MARKERS:
        assert marker not in dump, marker


def test_every_recommendation_column_has_an_explicit_retention_rule():
    """An allowlist, so a column added later is dropped (or fails loudly), never kept by accident."""
    columns = set(FragranceRecommendation.__table__.columns.keys())
    ruled = data_lifecycle._RETAINED_RECOMMENDATION_FIELDS | set(data_lifecycle._BLANK_VALUES) | {"conversationId"}
    assert columns == ruled, columns ^ ruled
    assert not data_lifecycle._RETAINED_RECOMMENDATION_FIELDS & {"customerProfileJson", "customerFacingJson", "draftName", "evidenceJson", "scoreJson"}


async def test_retained_record_cannot_be_reached_through_the_old_conversation_or_preview(world):
    mine = await world.conversation()
    saved = await world.recommendation(mine["conversationId"], build_status="saved", product_id="gid://shopify/Product/778")
    async with SessionLocal() as session:
        build_token = await issue_build_token(session, recommendation_id=saved, conversation_id=mine["conversationId"], shop="test-shop.myshopify.com")
    with TestClient(app) as client:
        assert _delete(client, mine).status_code == 200
        assert client.get("/chat", params={"history": "true", "conversation_id": mine["conversationId"]}, headers={CONVERSATION_TOKEN_HEADER: mine["conversationToken"]}).status_code == 401
        assert client.post("/api/save-build", json={"recommendationId": saved, "buildToken": build_token, "ratios": {"top": 34, "middle": 33, "base": 33}}).status_code == 403


# ===========================================================================
# 3. After deletion: capabilities, history, continuation, caches, model context
# ===========================================================================

async def test_after_deletion_nothing_works_and_nothing_comes_back(world, monkeypatch):
    mine = await world.conversation()
    cid = mine["conversationId"]
    conversation_flow._CONVERSATIONS[cid] = [{"role": "user", "content": MESSAGE}]          # a warm history cache ...
    tool_executor._conversation_scratch[cid] = {"lastGenerationAttemptHash": "x"}          # ... and scratch state
    seen = []

    async def _spy(messages, tools, tool_choice=None):
        seen.append(json.dumps(messages))
        return {"choices": [{"finish_reason": "stop", "message": {"content": None if tool_choice else "ok"}}]}

    monkeypatch.setattr(conversation_flow, "call_openai_once", _spy)
    with TestClient(app) as client:
        assert _delete(client, mine).status_code == 200
        assert cid not in conversation_flow._CONVERSATIONS and cid not in tool_executor._conversation_scratch
        headers = {CONVERSATION_TOKEN_HEADER: mine["conversationToken"]}
        assert client.get("/chat", params={"history": "true", "conversation_id": cid}, headers=headers).status_code == 401
        assert client.post("/chat", json={"conversation_id": cid, "message": "I love vanilla"}, headers=headers).status_code == 401
        assert _delete(client, mine).status_code == 401  # repeat after completion: the capability is gone; same refusal as anyone else
        internal = {"X-Internal-Api-Key": settings.internal_api_key}
        assert client.get("/internal/chat/history", params={"conversation_id": cid}, headers=internal).json() == {"messages": []}
        continued = client.post("/internal/chat", json={"conversation_id": cid, "message": "I love vanilla and sandalwood"}, headers=internal)
        new_id_event = next(json.loads(line[6:]) for line in continued.text.splitlines() if line.startswith("data: ") and '"id"' in line)
        assert new_id_event["conversation_id"] != cid  # the trusted adapter cannot resume it either: a fresh one is minted
        world.conversation_ids.append(new_id_event["conversation_id"])
    assert set((await _counts(cid)).values()) == {0}
    assert all(marker not in blob for blob in seen for marker in MARKERS)  # no deleted value reached a model


@pytest.mark.parametrize("writer", ["conversation_upsert", "message", "classified_message", "profile", "recommendation", "build_token", "draft"])
async def test_every_write_path_refuses_a_deleted_conversation_even_with_a_stale_cache(world, writer):
    """Simulates ANOTHER instance: it still holds the conversation in its own cache and never saw
    the deletion. The durable tombstone is what stops it writing the data back."""
    mine = await world.conversation()
    cid = mine["conversationId"]
    kept = await world.recommendation(cid, build_status="saved", product_id="gid://shopify/Product/779")
    async with SessionLocal() as session:
        await delete_conversation(session, cid)
    conversation_flow._CONVERSATIONS[cid] = [{"role": "user", "content": MESSAGE}]  # the other instance's stale cache
    async with SessionLocal() as session:
        with pytest.raises(ConversationDeleted):
            if writer == "conversation_upsert":
                await create_or_update_conversation(session, cid, EMAIL, NAME)
            elif writer == "message":
                await save_message(session, cid, "assistant", f"Hello again {NAME}")
            elif writer == "classified_message":
                await save_user_message_with_classification(session, cid, MESSAGE, classification="FRAGRANCE", reason_code="NONE", version="t")
            elif writer == "profile":
                await save_customer_profile_fields(session, cid, {"name": NAME, "city": CITY})
            elif writer == "recommendation":
                await save_recommendation(session, conversation_id=cid, profile={"name": NAME}, combination={"type": "HYBRID", "internalProducts": [{"title": "A", "notes": ["Rose"]}, {"title": "B", "notes": ["Amber"]}], "recommendedRatio": [{"productTitle": "A", "ratioPercent": 60}, {"productTitle": "B", "ratioPercent": 40}]})
            elif writer == "build_token":
                await issue_build_token(session, recommendation_id=kept, conversation_id=cid, shop="test-shop.myshopify.com")
            else:
                raise ConversationDeleted()  # draft: covered below (the record is detached, so the old id no longer reaches it)
    assert set((await _counts(cid)).values()) == {0}
    dump = await _database_dump()
    for marker in MARKERS:
        assert marker not in dump, marker


# ===========================================================================
# 4. Races (synchronization barriers, never sleeps)
# ===========================================================================

async def test_deletion_during_an_active_chat_turn_is_a_retryable_conflict_that_writes_nothing(world, monkeypatch):
    mine = await world.conversation()
    cid = mine["conversationId"]
    in_model, release = threading.Event(), threading.Event()
    completed_normally = []

    async def _paused_model(messages, tools, tool_choice=None):
        if tool_choice:
            return {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}
        in_model.set()
        await asyncio.to_thread(release.wait, 30)
        completed_normally.append(release.is_set())
        return {"choices": [{"finish_reason": "stop", "message": {"content": f"Here you go {NAME}"}}]}

    monkeypatch.setattr(conversation_flow, "call_openai_once", _paused_model)
    loop = asyncio.get_running_loop()
    with TestClient(app) as client:
        headers = {CONVERSATION_TOKEN_HEADER: mine["conversationToken"]}
        turn = loop.run_in_executor(None, lambda: client.post("/chat", json={"conversation_id": cid, "message": f"I love vanilla, I'm {NAME}"}, headers=headers))
        assert await loop.run_in_executor(None, in_model.wait, 20)  # the turn holds the conversation lock, mid-model-call

        conflict = await loop.run_in_executor(None, lambda: _delete(client, mine))
        assert conflict.status_code == 409 and conflict.json()["code"] == "deletion_conflict" and "Nothing has been deleted" in conflict.json()["error"]
        # NOTHING was written: no tombstone, capabilities intact, history still readable.
        async with SessionLocal() as session:
            assert await session.scalar(select(func.count()).select_from(ConversationDeletion).where(ConversationDeletion.conversationKey == conversation_key(cid))) == 0
            assert (await session.scalar(select(ConversationCapability.revokedAt).where(ConversationCapability.conversationId == cid))) is None
        assert (await loop.run_in_executor(None, lambda: client.get("/chat", params={"history": "true", "conversation_id": cid}, headers=headers))).status_code == 200

        release.set()
        finished = await turn
        assert completed_normally == [True] and finished.status_code == 200
        # The same credential simply retries once the turn is over.
        done = await loop.run_in_executor(None, lambda: _delete(client, mine))
        assert done.status_code == 200 and done.json()["status"] == "deleted"
    assert set((await _counts(cid)).values()) == {0} and cid not in conversation_flow._CONVERSATIONS
    dump = await _database_dump()
    for marker in MARKERS:
        assert marker not in dump, marker


async def test_deletion_during_a_build_operation_is_a_conflict_then_minimizes_the_build_record_afterwards(world):
    mine = await world.conversation()
    cid = mine["conversationId"]
    building = await world.recommendation(cid, build_status="creating")
    async with build_commerce_lock(building):  # a build operation is in flight on another worker
        async with SessionLocal() as session:
            result = await delete_conversation(session, cid)
        assert result.completed is False and result.state == "conflict"
        assert (await _counts(cid))["messages"] > 0 and (await _counts(cid))["capabilities"] == 1
    async with SessionLocal() as session:
        assert (await delete_conversation(session, cid)).completed is True
    assert set((await _counts(cid)).values()) == {0}
    async with SessionLocal() as session:
        row = await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == building))
        assert row.buildStatus == "creating" and row.conversationId.startswith(DETACHED_PREFIX) and row.draftName is None


async def test_lock_order_is_conversation_then_builds_and_everything_is_released_on_failure(world, monkeypatch):
    mine = await world.conversation()
    cid = mine["conversationId"]
    r1, r2 = sorted([await world.recommendation(cid), await world.recommendation(cid)])
    order = []
    real_turn, real_build = data_lifecycle.__dict__.get("conversation_turn_lock"), None
    import app.services.build_commerce as bc
    import app.services.turn_lock as tl

    real_turn_lock, real_build_lock = tl.conversation_turn_lock, bc.build_commerce_lock

    def _turn(conversation_id):
        order.append(("conversation", conversation_id))
        return real_turn_lock(conversation_id)

    def _build(recommendation_id):
        order.append(("build", recommendation_id))
        return real_build_lock(recommendation_id)

    monkeypatch.setattr(tl, "conversation_turn_lock", _turn)
    monkeypatch.setattr(bc, "build_commerce_lock", _build)

    async def _boom(session, conversation_id):
        raise RuntimeError("purge failed")

    monkeypatch.setattr(data_lifecycle, "_purge", _boom)
    async with SessionLocal() as session:
        with pytest.raises(RuntimeError):
            await delete_conversation(session, cid)
    assert order == [("conversation", cid), ("build", r1), ("build", r2)]
    assert real_turn is None and real_build is None
    # ATOMIC: nothing was removed, no tombstone, capabilities untouched, every lock released.
    assert (await _counts(cid))["messages"] > 0 and (await _counts(cid))["capabilities"] == 1
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(ConversationDeletion).where(ConversationDeletion.conversationKey == conversation_key(cid))) == 0
    async with conversation_turn_lock(cid):
        async with build_commerce_lock(r1), build_commerce_lock(r2):
            pass
    monkeypatch.undo()  # also undoes the autouse review-flag patch, so it is set again explicitly
    monkeypatch.setattr(settings, "shared_data_deletion_reviewed", True)
    async with SessionLocal() as session:  # a retry with the still-valid credential completes it
        assert (await delete_conversation(session, cid)).completed is True


async def test_repeated_and_concurrent_deletion_requests_are_idempotent(world):
    mine = await world.conversation()
    cid = mine["conversationId"]

    async def _once():
        async with SessionLocal() as session:
            return await delete_conversation(session, cid)

    results = await asyncio.gather(_once(), _once(), _once(), return_exceptions=True)
    # Exactly one completes; the others either see it already done (completed, already_deleted) or
    # conflict (nothing written) because the winner held the locks. Never an exception, never two purges.
    assert all(not isinstance(r, Exception) for r in results), results
    assert sum(1 for r in results if r.completed and not r.already_deleted) == 1
    async with SessionLocal() as session:
        final = await delete_conversation(session, cid)
    assert final.completed is True and final.already_deleted is True and set((await _counts(cid)).values()) == {0}
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(ConversationDeletion).where(ConversationDeletion.conversationKey == conversation_key(cid))) == 1


# ===========================================================================
# 5. N14: the history read changes nothing
# ===========================================================================

async def test_history_read_is_read_only_and_cannot_resurrect_or_extend_anything(world):
    mine = await world.conversation(age_days=10)
    cid = mine["conversationId"]
    async with SessionLocal() as session:
        await save_customer_profile_fields(session, cid, {"pendingRecreateRecommendationId": "rec-x"})
        before_rows = await _database_dump()
        updated_before = await session.scalar(select(Conversation.updatedAt).where(Conversation.id == cid))
    with TestClient(app) as client:
        headers = {CONVERSATION_TOKEN_HEADER: mine["conversationToken"]}
        for _ in range(3):
            assert client.get("/chat", params={"history": "true", "conversation_id": cid}, headers=headers).status_code == 200
        internal = client.get("/internal/chat/history", params={"conversation_id": cid}, headers={"X-Internal-Api-Key": settings.internal_api_key})
        assert internal.status_code == 200
    assert await _database_dump() == before_rows  # no message appended, no profile rewritten, no conversation upserted
    async with SessionLocal() as session:
        assert await session.scalar(select(Conversation.updatedAt).where(Conversation.id == cid)) == updated_before
        assert (await get_customer_profile(session, cid))["pendingRecreateRecommendationId"] == "rec-x"  # consumed only by an explicit POST turn


async def test_recreate_marker_is_consumed_by_the_next_explicit_turn_and_the_appended_prompt_is_visible(world):
    mine = await world.conversation()
    cid = mine["conversationId"]
    conversation_flow._CONVERSATIONS[cid] = [{"role": "user", "content": "stale cached copy"}]  # this process's cache predates the recreate POST
    async with SessionLocal() as session:
        await save_message(session, cid, "assistant", chat_module.RECREATE_REENTRY_MESSAGE)  # what the recreate POST does
        await save_customer_profile_fields(session, cid, {"pendingRecreateRecommendationId": "rec-x"})
    with TestClient(app) as client:
        headers = {CONVERSATION_TOKEN_HEADER: mine["conversationToken"]}
        messages = client.get("/chat", params={"history": "true", "conversation_id": cid}, headers=headers).json()["messages"]
        assert messages[-1]["content"] == chat_module.RECREATE_REENTRY_MESSAGE  # fresh read, despite the stale cache
        assert client.post("/chat", json={"conversation_id": cid, "message": "make it sweeter with more vanilla"}, headers=headers).status_code == 200
    async with SessionLocal() as session:
        assert (await get_customer_profile(session, cid))["pendingRecreateRecommendationId"] is None


# ===========================================================================
# 6. Retention
# ===========================================================================

@pytest.fixture
def retention_enabled(monkeypatch):
    monkeypatch.setattr(settings, "retention_execution_enabled", True)


async def _run(**kw):
    async with SessionLocal() as session:
        return await run_retention(session, **kw)


async def test_dry_run_is_the_default_and_writes_nothing(world, retention_enabled):
    old = await world.conversation(age_days=200)
    before = await _database_dump()
    report = await _run()
    assert report.dry_run is True and report.eligible["inactiveConversations"] >= 1 and report.deleted == {} and report.minimized == 0
    assert await _database_dump() == before and (await _counts(old["conversationId"]))["conversation"] == 1


async def test_execution_is_impossible_until_an_operator_enables_it(world, monkeypatch):
    old = await world.conversation(age_days=200)
    monkeypatch.setattr(settings, "retention_execution_enabled", False)
    report = await _run(execute=True)
    assert report.dry_run is True and report.deleted == {}
    assert (await _counts(old["conversationId"]))["conversation"] == 1
    assert settings.__class__.model_fields["retention_execution_enabled"].default is False


async def test_cutoff_boundary_and_activity_definition(world, retention_enabled, monkeypatch):
    now = utcnow()
    monkeypatch.setattr(settings, "retention_inactive_conversation_days", 90)
    just_inside = await world.conversation(age_days=400, last_customer_message_days=89.99)   # old conversation, recent CUSTOMER message
    just_outside = await world.conversation(age_days=400, last_customer_message_days=90.01)
    no_messages_old = await world.conversation(age_days=91, with_data=False)
    polled = await world.conversation(age_days=400, last_customer_message_days=200)
    async with SessionLocal() as session:  # background activity that must NOT extend retention
        convo = await session.scalar(select(Conversation).where(Conversation.id == polled["conversationId"]))
        convo.updatedAt = now
        session.add(Message(id=new_id(), conversationId=polled["conversationId"], role="assistant", content="server-side note", createdAt=now))
        capability = await session.scalar(select(ConversationCapability).where(ConversationCapability.conversationId == polled["conversationId"]))
        capability.lastUsedAt = now
        await session.commit()
    with TestClient(app) as client:
        assert client.get("/chat", params={"history": "true", "conversation_id": polled["conversationId"]}, headers={CONVERSATION_TOKEN_HEADER: polled["conversationToken"]}).status_code == 200

    report = await _run(execute=True, now=now)
    assert report.dry_run is False and report.failed == 0
    assert (await _counts(just_inside["conversationId"]))["conversation"] == 1
    for gone in (just_outside, no_messages_old, polled):
        assert set((await _counts(gone["conversationId"])).values()) == {0}
    assert report.cutoffs["inactiveConversation"].endswith("Z")


async def test_batches_are_bounded_progress_is_stable_and_repeat_runs_are_safe(world, retention_enabled):
    created = [await world.conversation(age_days=300 + i, with_data=False) for i in range(5)]
    first = await _run(execute=True, batch_size=2, max_batches=1)
    assert first.batches == 1 and first.deleted.get("conversations") == 2 and first.more_remaining is True
    remaining = [c for c in created if (await _counts(c["conversationId"]))["conversation"] == 1]
    assert len(remaining) == 3 and created[-1] not in remaining and created[-2] not in remaining  # oldest first: deterministic order
    second = await _run(execute=True, batch_size=2, max_batches=10)
    assert second.deleted.get("conversations") == 3 and second.more_remaining is False
    third = await _run(execute=True, batch_size=2)
    assert third.deleted.get("conversations") is None and third.failed == 0  # idempotent


async def test_only_one_retention_run_at_a_time(world, retention_enabled):
    old = await world.conversation(age_days=300, with_data=False)
    from app.db.session import engine

    connection = await engine.connect()
    try:
        assert await connection.scalar(text("SELECT pg_try_advisory_lock(3, 0)")) is True  # another worker is running
        report = await _run(execute=True)
        assert report.held == {"anotherRetentionRunActive": 1} and report.deleted == {}
        assert (await _counts(old["conversationId"]))["conversation"] == 1
    finally:
        await connection.execute(text("SELECT pg_advisory_unlock(3, 0)"))
        await connection.close()
    assert (await _run(execute=True)).deleted.get("conversations", 0) >= 1


async def test_partial_failure_is_reported_and_never_counted_as_deleted(world, retention_enabled, monkeypatch):
    good = await world.conversation(age_days=300, with_data=False)
    bad = await world.conversation(age_days=301, with_data=False)
    real = data_lifecycle.delete_conversation

    async def _flaky(session, conversation_id, **kw):
        if conversation_id == bad["conversationId"]:
            raise RuntimeError(f"boom for {NAME}")
        return await real(session, conversation_id, **kw)

    monkeypatch.setattr(data_lifecycle, "delete_conversation", _flaky)
    report = await _run(execute=True)
    assert report.failed == 1 and report.deleted.get("conversations") >= 1
    assert (await _counts(bad["conversationId"]))["conversation"] == 1 and (await _counts(good["conversationId"]))["conversation"] == 0
    assert NAME not in json.dumps(report.as_dict())


async def test_held_commerce_records_are_counted_and_preserved_and_released_only_when_resolved(world, retention_enabled, monkeypatch):
    mine = await world.conversation(age_days=500)
    cid = mine["conversationId"]
    unresolved = await world.recommendation(cid, build_status="pending_review", age_days=500)
    resolved = await world.recommendation(cid, build_status="saved", product_id="gid://shopify/Product/780", age_days=500)
    recent = await world.recommendation(cid, build_status="saved", product_id="gid://shopify/Product/781", age_days=10)
    monkeypatch.setattr(settings, "retention_commerce_record_days", 365)
    first = await _run(execute=True)
    # The conversation expires: all three commerce records are minimized. The old RESOLVED one is
    # already past its own period, so the same run removes it. The unresolved one is HELD.
    assert first.minimized == 3 and first.deleted.get("commerceRecords") == 1 and first.held.get("unresolvedCommerceRecords") == 1
    second = await _run(execute=True)  # nothing new to do; the hold is still reported, never silently dropped
    assert second.deleted.get("commerceRecords") is None and second.held.get("unresolvedCommerceRecords") == 1
    async with SessionLocal() as session:
        assert await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == resolved)) is None
        assert (await session.scalar(select(FragranceRecommendation.buildStatus).where(FragranceRecommendation.id == unresolved))) == "pending_review"  # never auto-removed
        assert await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == recent)) is not None


async def test_dead_capabilities_buckets_and_tombstones_follow_their_own_clocks(world, retention_enabled):
    live = await world.conversation()
    cid = live["conversationId"]
    async with SessionLocal() as session:
        now = utcnow()
        session.add(ConversationCapability(id=new_id(), conversationId=cid, tokenHash=f"dead-{uuid.uuid4().hex}", expiresAt=now - timedelta(days=30), createdAt=now - timedelta(days=60)))
        session.add(ConversationCapability(id=new_id(), conversationId=cid, tokenHash=f"revoked-{uuid.uuid4().hex}", expiresAt=now + timedelta(days=30), revokedAt=now - timedelta(days=8), createdAt=now - timedelta(days=9)))
        session.add(ConversationCapability(id=new_id(), conversationId=cid, tokenHash=f"justexpired-{uuid.uuid4().hex}", expiresAt=now - timedelta(days=1), createdAt=now - timedelta(days=9)))
        session.add(RateLimitBucket(key=f"pytest-f11-old:{uuid.uuid4().hex}", windowStart=now - timedelta(days=9), count=1, updatedAt=now - timedelta(days=9)))
        session.add(ConversationDeletion(conversationKey=f"pytest-f11-{uuid.uuid4().hex}", origin="customer", completedAt=now - timedelta(days=40), expiresAt=now - timedelta(days=33), heldRecords=0))
        session.add(ConversationDeletion(conversationKey=f"pytest-f11-{uuid.uuid4().hex}", origin="customer", completedAt=now, expiresAt=now + timedelta(days=7), heldRecords=0))
        await session.commit()
    report = await _run(execute=True)
    assert report.deleted.get("conversationCapabilities") == 2 and report.deleted.get("rateLimitBuckets", 0) >= 1 and report.deleted.get("tombstones", 0) >= 1
    async with SessionLocal() as session:
        hashes = set((await session.execute(select(ConversationCapability.tokenHash).where(ConversationCapability.conversationId == cid))).scalars())
        assert any(h.startswith("justexpired-") for h in hashes) and not any(h.startswith(("dead-", "revoked-")) for h in hashes)
        assert await session.scalar(select(func.count()).select_from(ConversationDeletion).where(ConversationDeletion.conversationKey.like("pytest-f11-%"))) == 1
        await session.execute(delete(ConversationDeletion).where(ConversationDeletion.conversationKey.like("pytest-f11-%")))
        await session.commit()
    # Capability expiry is NOT deletion of the customer's data: the live conversation is untouched.
    assert (await _counts(cid))["messages"] == 2


def test_maintenance_command_output_is_counts_only_even_on_failure(monkeypatch, capsys):
    from scripts import data_retention

    async def _explode(*a, **kw):
        raise RuntimeError(f"postgresql://user:SuperSecretPassw0rd@db.internal/prod {EMAIL} {NAME}")

    monkeypatch.setattr(data_lifecycle, "run_retention", _explode)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = data_retention.main([])
    printed = out.getvalue() + err.getvalue() + "".join(capsys.readouterr())
    assert code == 1 and json.loads(out.getvalue()) == {"ok": False, "errorType": "RuntimeError"}
    for secret in ("SuperSecretPassw0rd", "postgresql://", EMAIL, NAME):
        assert secret not in printed


async def test_maintenance_command_dry_run_end_to_end(world, monkeypatch):
    await world.conversation(age_days=300)
    from scripts import data_retention

    out = io.StringIO()
    with redirect_stdout(out):
        code = await data_retention._main(False, 50, 5)
    summary = json.loads(out.getvalue())
    assert code == 0 and summary["dryRun"] is True and summary["executionRequested"] is False and summary["deleted"] == {}
    assert set(summary) == {"ok", "dryRun", "executionRequested", "cutoffsUtc", "eligible", "deleted", "minimized", "held", "failed", "batches", "moreRemaining"}
    for marker in MARKERS:
        assert marker not in out.getvalue()


def test_retention_is_not_reachable_over_http_and_not_scheduled():
    paths = " ".join(app.openapi()["paths"]).lower()
    assert "retention" not in paths and "maintenance" not in paths and "cleanup" not in paths
    render = open("render.yaml").read().lower()
    assert "cron" not in render and "data_retention" not in render


# ===========================================================================
# 7. Logs and errors
# ===========================================================================

SECRET_TOKEN = "tok_SECRET_CAPABILITY_MARKER_9f31c2"


def test_exception_summaries_never_contain_exception_messages():
    try:
        try:
            raise ValueError(f"{EMAIL} {SECRET_TOKEN} postgresql://u:SuperSecretPassw0rd@h/db")
        except ValueError as inner:
            raise RuntimeError(f"wrapping {NAME}") from inner
    except RuntimeError as err:
        summary = safe_exception_summary(err)
        record = logging.LogRecord("app.test", logging.ERROR, __file__, 1, "Action error: %s", (type(err).__name__,), (type(err), err, err.__traceback__))
    rendered = JsonFormatter().format(record)
    assert summary["types"] == ["RuntimeError", "ValueError"] and summary["frames"]
    for secret in (EMAIL, SECRET_TOKEN, "SuperSecretPassw0rd", NAME, "postgresql://"):
        assert secret not in rendered and secret not in json.dumps(summary)


async def test_database_errors_do_not_carry_bound_values(world):
    from sqlalchemy.exc import DBAPIError

    async with SessionLocal() as session:
        with pytest.raises(DBAPIError) as err:
            await session.execute(text('INSERT INTO "Message" ("id", "conversationId", "role", "content", "createdAt") VALUES (:i, :c, :r, :content, now())'),
                                  {"i": new_id(), "c": "missing-conversation", "r": "user", "content": f"{MESSAGE} {EMAIL}"})
        await session.rollback()
    assert EMAIL not in str(err.value) and "Quillona" not in str(err.value)


async def test_turns_deletions_and_failures_log_no_customer_content_tokens_or_raw_paths(world, caplog, monkeypatch):
    mine = await world.conversation()
    cid = mine["conversationId"]

    async def _exploding_model(messages, tools, tool_choice=None):
        raise RuntimeError(f"upstream echoed: {messages!r} {SECRET_TOKEN}")

    with TestClient(app) as client, caplog.at_level(logging.DEBUG):
        headers = {CONVERSATION_TOKEN_HEADER: mine["conversationToken"]}
        client.post("/chat", json={"conversation_id": cid, "message": f"I love vanilla. {MESSAGE}", "customer_name": NAME, "customer_email": EMAIL}, headers=headers)
        monkeypatch.setattr(conversation_flow, "call_openai_once", _exploding_model)
        failed = client.post("/chat", json={"conversation_id": cid, "message": f"more vanilla please, {EMAIL}"}, headers=headers)
        client.get(f"/chat?history=true&conversation_id={cid}&note={EMAIL}", headers=headers)
        client.get(f"/internal/recommendations/{SECRET_TOKEN}", headers={"X-Internal-Api-Key": settings.internal_api_key})
        deleted = _delete(client, mine)
    formatter = JsonFormatter()
    # `httpx` records here come from the TEST CLIENT object, not from the application (the
    # application's own httpx logger is silenced below INFO; see test_outbound_http_clients...).
    logs = "\n".join(formatter.format(r) for r in caplog.records if not r.name.startswith("httpx"))
    for secret in (*MARKERS, mine["conversationToken"], SECRET_TOKEN, "upstream echoed"):
        assert secret not in logs, secret
    assert "/internal/recommendations/{" in logs or "unmatched" in logs  # the route template, not the raw path
    assert "error" in failed.text and "upstream" not in failed.text and "RuntimeError" not in failed.text and "Traceback" not in failed.text
    assert deleted.status_code == 200 and cid not in deleted.text


def test_model_provider_errors_are_logged_without_bodies(caplog, monkeypatch):
    import httpx

    from app.ai import openai_client

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, *a, **kw):
            return httpx.Response(400, text=f'{{"error": "bad request echoing {EMAIL} {MESSAGE}"}}', request=httpx.Request("POST", "https://synthetic.invalid"))

    monkeypatch.setattr(openai_client.httpx, "AsyncClient", _Client)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(REAL_CALL_OPENAI_ONCE([{"role": "user", "content": MESSAGE}], None)) is None
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "status=400" in logs and EMAIL not in logs and "Quillona" not in logs


async def test_discovery_inventory_logs_carry_no_titles_item_codes_or_quantities(caplog, monkeypatch):
    from app.services import odoo_inventory

    async def _skus(session, titles):
        return {t: {"fragranceProductId": "p", "odooSku": "PRIVATE-ITEM-CODE-55"} for t in titles}

    async def _lookup(session, titles):
        return {"results": {t: {"mappingStatus": "CONNECTED", "availableOilMl": 4321.5, "odooSku": "PRIVATE-ITEM-CODE-55", "fragranceProductId": "p"} for t in titles}, "requestCount": 1, "skusQueried": ["PRIVATE-ITEM-CODE-55"]}

    monkeypatch.setattr(odoo_inventory, "resolve_odoo_skus_for_titles", _skus)
    monkeypatch.setattr(odoo_inventory, "get_oil_inventory_for_product_titles", _lookup)
    with caplog.at_level(logging.DEBUG):
        result = await odoo_inventory.evaluate_candidate_inventory(None, {"recommendationId": "r1", "recommendedRatio": [{"productTitle": PRIVATE_TITLE, "ratioPercent": 100}]})
    assert result["inventoryValidated"] is True
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "ODOO_INVENTORY_RESPONSE" in logs
    for private in (PRIVATE_TITLE, "PRIVATE-ITEM-CODE-55", "4321.5"):
        assert private not in logs, private


# ===========================================================================
# 8. Network and regression
# ===========================================================================

async def test_deletion_and_retention_make_no_external_call(world, retention_enabled, network_guard, monkeypatch):
    from app.integrations import odoo_client
    from app.shopify import admin_client

    async def _never(*a, **kw):
        raise AssertionError("deletion/retention must not call an external service")

    monkeypatch.setattr(admin_client, "admin_graphql", _never)
    monkeypatch.setattr(odoo_client, "get_inventory_by_skus", _never)
    monkeypatch.setattr(conversation_flow, "call_openai_once", _never)
    mine = await world.conversation()
    await world.recommendation(mine["conversationId"], build_status="saved", product_id="gid://shopify/Product/782")
    await world.conversation(age_days=300)
    attempts = len(network_guard.attempts)
    with TestClient(app) as client:
        assert _delete(client, mine).status_code == 200
    assert (await _run(execute=True)).failed == 0
    assert len(network_guard.attempts) == attempts


async def test_ordinary_guest_flow_is_unchanged(world):
    with TestClient(app) as client:
        session_data = client.post("/chat/session", json={}).json()
        world.conversation_ids.append(session_data["conversationId"])
        headers = {CONVERSATION_TOKEN_HEADER: session_data["conversationToken"]}
        first = client.post("/chat", json={"conversation_id": session_data["conversationId"], "message": "I love vanilla and sandalwood for winter"}, headers=headers)
        second = client.post("/chat", json={"conversation_id": session_data["conversationId"], "message": "make it a little sweeter"}, headers=headers)
        history = client.get("/chat", params={"history": "true", "conversation_id": session_data["conversationId"]}, headers=headers)
    assert first.status_code == second.status_code == history.status_code == 200
    assert [m["role"] for m in history.json()["messages"]] == ["user", "assistant", "user", "assistant"]


async def test_draft_save_refuses_a_deleted_or_minimized_record(world):
    mine = await world.conversation()
    kept = await world.recommendation(mine["conversationId"], build_status="saved", product_id="gid://shopify/Product/790")
    async with SessionLocal() as session:
        assert (await delete_conversation(session, mine["conversationId"])).completed
    async with SessionLocal() as session:
        with pytest.raises(ConversationDeleted):  # the retained record accepts no further customer write
            await mark_recommendation_draft(session, kept, name=BLEND_NAME, ratios={"top": 34, "middle": 33, "base": 33})
        assert (await session.scalar(select(FragranceRecommendation.draftName).where(FragranceRecommendation.id == kept))) is None


def test_outbound_http_client_loggers_cannot_emit_request_urls():
    """httpx logs every outbound URL at INFO; the inventory lookup carries private item codes in
    its query string. After the application configures logging those loggers are WARNING+."""
    from app.logging_config import configure_logging

    configure_logging("DEBUG")
    try:
        for name in ("httpx", "httpcore"):
            assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
            assert not logging.getLogger(name).isEnabledFor(logging.INFO)
    finally:
        configure_logging(settings.log_level if hasattr(settings, "log_level") else "INFO")
