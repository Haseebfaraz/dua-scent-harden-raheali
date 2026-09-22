# ruff: noqa: F811  -- pytest fixtures imported from the Phase 6 suite are re-declared as test parameters by design
"""Phase 6A regressions: a truthful deletion contract, database-level resurrection protection,
shared-data safety, tombstone expiry, and deletion around in-flight commerce.

Synthetic records only, in the disposable local PostgreSQL. Every race uses two or more REAL
database sessions and synchronization barriers, never sleeps, and never mocks the guard.
"""

import asyncio
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.ai import conversation_flow
from app.config import Settings, settings
from app.db.ids import new_id
from app.db.models import ConversationCapability, ConversationDeletion, CustomerAccountUrls, FragranceRecommendation, Message, OrderHistory
from app.db.session import SessionLocal
from app.db.time import utcnow
from app.main import app
from app.services import build_commerce, data_lifecycle
from app.services.build_capability import issue_build_token
from app.services.build_commerce import BuildPendingReview, execute_build_commerce
from app.services.conversation import create_or_update_conversation, save_message, save_user_message_with_classification
from app.services.conversation_capability import CONVERSATION_TOKEN_HEADER
from app.services.customer_profile import save_customer_profile_fields
from app.services.data_lifecycle import ConversationDeleted, conversation_key, delete_conversation, run_retention
from app.services.recommendation_confirmation import mark_recommendation_draft, save_recommendation
from app.shopify import builds
from tests.security.test_commerce_inventory import ShopifyWrites
from tests.security.test_data_lifecycle import (  # noqa: F401 -- fixtures re-used on purpose
    BLEND_NAME, EMAIL, MARKERS, MESSAGE, NAME, World, _counts, _database_dump, _delete, _model, world,
)

SHOP = "test-shop.myshopify.com"
RATIOS = {"top": 34, "middle": 33, "base": 33}


@pytest.fixture(autouse=True)
def _deletion_enabled(monkeypatch):
    monkeypatch.setattr(settings, "shared_data_deletion_reviewed", True)


@pytest.fixture(autouse=True)
def _commerce_contract(monkeypatch):
    """The synthetic inventory contract from the Phase 5 suites, so builds can be exercised."""
    monkeypatch.setattr(settings, "shopify_shop_domain", SHOP)
    monkeypatch.setattr(settings, "odoo_inventory_url", "https://odoo.synthetic.invalid/api/get-inventory")
    monkeypatch.setattr(settings, "odoo_inventory_location_scope", "SYNTH/Stock")
    monkeypatch.setattr(settings, "odoo_inventory_quantity_semantics", "UNRESERVED_AVAILABLE")
    monkeypatch.setattr(settings, "manufacturing_max_oil_ml_per_bottle", 14.0)

    async def _verified(session, *a, **kw):
        return None

    monkeypatch.setattr("app.shopify.builds.require_commerce_inventory", _verified)
    monkeypatch.setattr("app.shopify.builds.ensure_still_satisfied", _verified)


async def _tombstones(cid: str) -> int:
    async with SessionLocal() as session:
        return await session.scalar(select(func.count()).select_from(ConversationDeletion).where(ConversationDeletion.conversationKey == conversation_key(cid)))


# ===========================================================================
# 1. Acceptance is truthful: 200 = committed, 409 = nothing written, no other state
# ===========================================================================

def test_no_pending_state_exists_anywhere():
    import inspect

    source = inspect.getsource(data_lifecycle)
    assert "STATE_DELETING" not in source and "pendingConversationId" not in source and "complete_pending" not in source
    chat_source = inspect.getsource(__import__("app.api.chat", fromlist=["x"]))
    assert "202" not in chat_source and "deletion_pending" not in chat_source
    assert not hasattr(data_lifecycle, "complete_pending_deletion") and not hasattr(data_lifecycle, "request_conversation_deletion")
    assert set(ConversationDeletion.__table__.columns.keys()) == {"conversationKey", "origin", "completedAt", "expiresAt", "heldRecords"}


async def test_route_contract_200_conflict_and_retry(world):
    mine = await world.conversation()
    cid = mine["conversationId"]
    from app.services.turn_lock import conversation_turn_lock

    with TestClient(app) as client:
        async with conversation_turn_lock(cid):  # something is in flight on another worker
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(None, lambda: _delete(client, mine))
            assert response.status_code == 409 and response.json()["code"] == "deletion_conflict" and response.headers.get("Retry-After")
            assert await _tombstones(cid) == 0 and (await _counts(cid))["capabilities"] == 1
            async with SessionLocal() as session:  # not even a revocation
                assert (await session.scalar(select(ConversationCapability.revokedAt).where(ConversationCapability.conversationId == cid))) is None
        done = _delete(client, mine)  # the SAME credential retries
        assert done.status_code == 200 and done.json()["status"] == "deleted"
        assert set(done.json()) == {"status", "message", "notCovered", "commerceRecordRetained"}
        assert "backups" in done.json()["notCovered"] and "store" in done.json()["notCovered"]
        assert _delete(client, mine).status_code == 401  # and afterwards the credential no longer exists
    assert set((await _counts(cid)).values()) == {0} and await _tombstones(cid) == 1


async def test_a_completed_deletion_survives_a_restart_and_needs_no_retention(world, monkeypatch):
    """Everything is one committed transaction. A 'restart' (new sessions, cleared caches, the
    age-based retention flag off) changes nothing: the data is gone and stays gone."""
    monkeypatch.setattr(settings, "retention_execution_enabled", False)
    mine = await world.conversation()
    cid = mine["conversationId"]
    conversation_flow._CONVERSATIONS[cid] = [{"role": "user", "content": MESSAGE}]
    async with SessionLocal() as session:
        assert (await delete_conversation(session, cid)).completed
    conversation_flow._CONVERSATIONS.clear()  # process restart
    async with SessionLocal() as session:
        assert (await run_retention(session, execute=True)).dry_run is True  # retention cannot even run destructively
    assert set((await _counts(cid)).values()) == {0} and await _tombstones(cid) == 1
    dump = await _database_dump()
    for marker in MARKERS:
        assert marker not in dump, marker


async def test_a_failure_inside_the_transaction_leaves_nothing_behind_and_the_credential_still_works(world, monkeypatch):
    mine = await world.conversation()
    cid = mine["conversationId"]
    real = data_lifecycle._purge

    async def _partial_then_fail(session, conversation_id):
        await real(session, conversation_id)  # rows are deleted inside the transaction ...
        raise RuntimeError("database interruption after the purge, before the commit")

    monkeypatch.setattr(data_lifecycle, "_purge", _partial_then_fail)
    with TestClient(app) as client:
        failed = _delete(client, mine)
        assert failed.status_code == 503 and failed.json()["code"] == "deletion_failed" and "Nothing has been deleted" in failed.json()["error"]
        # ... and all of it is still there, with the credential intact and no tombstone.
        assert (await _counts(cid))["messages"] == 2 and (await _counts(cid))["capabilities"] == 1 and await _tombstones(cid) == 0
        assert client.get("/chat", params={"history": "true", "conversation_id": cid}, headers={CONVERSATION_TOKEN_HEADER: mine["conversationToken"]}).status_code == 200
        monkeypatch.setattr(data_lifecycle, "_purge", real)
        assert _delete(client, mine).status_code == 200


# ===========================================================================
# 2. The fail-closed gate for the shared-database review
# ===========================================================================

async def test_deletion_is_off_by_default_and_the_gate_changes_nothing_before_any_irreversible_step(world, monkeypatch):
    assert Settings.model_fields["shared_data_deletion_reviewed"].default is False
    monkeypatch.setattr(settings, "shared_data_deletion_reviewed", False)
    mine = await world.conversation()
    cid = mine["conversationId"]
    with TestClient(app) as client:
        response = _delete(client, mine)
        assert response.status_code == 503 and response.json()["code"] == "deletion_unavailable" and "Nothing has been changed" in response.json()["error"]
        assert client.get("/chat", params={"history": "true", "conversation_id": cid}, headers={CONVERSATION_TOKEN_HEADER: mine["conversationToken"]}).status_code == 200
        unauthorized = client.post("/chat/delete", json={"conversation_id": cid})
        assert unauthorized.status_code == 401  # authorization is still checked FIRST: the gate reveals nothing to strangers
    assert (await _counts(cid))["messages"] == 2 and (await _counts(cid))["capabilities"] == 1 and await _tombstones(cid) == 0


def test_the_review_the_gate_stands_for_is_documented():
    text_ = " ".join(open("docs/DATA_RETENTION_AND_DELETION.md").read().split())
    assert "SHARED_DATA_DELETION_REVIEWED" in text_ and "is not the review" in text_
    for table in ("Conversation", "Message", "CustomerProfileState", "FragranceRecommendation", "CustomerAccountUrls", "OrderHistory"):
        assert table in text_


# ===========================================================================
# 3. Shared records
# ===========================================================================

async def test_foreign_and_operational_records_are_never_touched(world):
    mine = await world.conversation()
    cid = mine["conversationId"]
    async with SessionLocal() as session:
        session.add(OrderHistory(id=new_id(), notes="Rose", city="Markerville", customerKeyHash="hash-of-" + NAME, createdAt=utcnow()))
        await session.commit()
        orders_before = await session.scalar(select(func.count()).select_from(OrderHistory))
        urls_before = await session.scalar(select(func.count()).select_from(CustomerAccountUrls).where(CustomerAccountUrls.conversationId == cid))
    async with SessionLocal() as session:
        assert (await delete_conversation(session, cid)).completed
        assert await session.scalar(select(func.count()).select_from(OrderHistory)) == orders_before
        assert await session.scalar(select(func.count()).select_from(CustomerAccountUrls).where(CustomerAccountUrls.conversationId == cid)) == urls_before == 1
        await session.execute(text('DELETE FROM "OrderHistory" WHERE "customerKeyHash" = :h').bindparams(h="hash-of-" + NAME))
        await session.execute(text('DELETE FROM "CustomerAccountUrls" WHERE "conversationId" = :c').bindparams(c=cid))
        await session.commit()


async def test_minimized_record_grants_no_access_and_accepts_no_customer_operation(world, monkeypatch):
    mine = await world.conversation()
    cid = mine["conversationId"]
    kept = await world.recommendation(cid, build_status="saved", product_id="gid://shopify/Product/801")
    async with SessionLocal() as session:
        token = await issue_build_token(session, recommendation_id=kept, conversation_id=cid, shop=SHOP)
        assert (await delete_conversation(session, cid)).completed
    shopify = ShopifyWrites(monkeypatch, recommendation_id=kept)
    with TestClient(app) as client:
        assert client.post("/api/save-build", json={"recommendationId": kept, "buildToken": token, "ratios": RATIOS}).status_code == 403
    async with SessionLocal() as session:  # a direct service caller with the id gets the same refusal, before any lock or read
        with pytest.raises(ConversationDeleted):
            await execute_build_commerce(session, SHOP, recommendation_id=kept, ratios=RATIOS, name=BLEND_NAME, save_draft=True)
        with pytest.raises(ConversationDeleted):
            await build_commerce.save_recreate_draft(session, recommendation_id=kept, name=BLEND_NAME, ratios=RATIOS)
        with pytest.raises(ConversationDeleted):
            await mark_recommendation_draft(session, kept, name=BLEND_NAME, ratios=RATIOS)
    assert shopify.calls == []
    async with SessionLocal() as session:
        row = await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == kept))
        assert row.draftName is None and row.shopifyProductId == "gid://shopify/Product/801" and row.buildStatus == "saved"


# ===========================================================================
# 4. The write-guard race, at the database level, for every writer
# ===========================================================================

class PausedWriter:
    """Runs one writer in ITS OWN database session and pauses it right after the guard passed,
    before its write is committed. The pause is a barrier the test controls."""

    def __init__(self, monkeypatch):
        self.guard_passed = asyncio.Event()
        self.release = asyncio.Event()
        real_guard = data_lifecycle.ensure_conversation_writable
        me = self

        async def _paused_guard(session, conversation_id):
            await real_guard(session, conversation_id)  # shared lock taken, no tombstone seen
            if not me.guard_passed.is_set():
                me.guard_passed.set()
                await me.release.wait()  # ... and now deletion is attempted from another session

        for module in ("app.services.conversation", "app.services.customer_profile", "app.services.recommendation_confirmation", "app.services.build_capability"):
            monkeypatch.setattr(f"{module}.ensure_conversation_writable", _paused_guard)


WRITERS = {
    "conversation_upsert": lambda s, cid, rec: create_or_update_conversation(s, cid, EMAIL, NAME),
    "message": lambda s, cid, rec: save_message(s, cid, "assistant", f"Lovely, {NAME}"),
    "classified_message": lambda s, cid, rec: save_user_message_with_classification(s, cid, MESSAGE, classification="FRAGRANCE", reason_code="NONE", version="t"),
    "profile": lambda s, cid, rec: save_customer_profile_fields(s, cid, {"name": NAME, "city": "Markerville"}),
    "recommendation": lambda s, cid, rec: save_recommendation(s, conversation_id=cid, profile={"name": NAME}, combination={"type": "HYBRID", "internalProducts": [{"title": "A", "notes": ["Rose"]}, {"title": "B", "notes": ["Amber"]}], "recommendedRatio": [{"productTitle": "A", "ratioPercent": 60}, {"productTitle": "B", "ratioPercent": 40}]}),
    "build_capability": lambda s, cid, rec: issue_build_token(s, recommendation_id=rec, conversation_id=cid, shop=SHOP),
    "draft": lambda s, cid, rec: mark_recommendation_draft(s, rec, name=BLEND_NAME, ratios=RATIOS),
}


@pytest.mark.parametrize("writer", sorted(WRITERS))
async def test_a_writer_paused_after_its_guard_cannot_restore_data_after_deletion(world, monkeypatch, writer):
    mine = await world.conversation(with_data=True)
    cid = mine["conversationId"]
    rec = await world.recommendation(cid)
    paused = PausedWriter(monkeypatch)

    async def _write():
        async with SessionLocal() as session:  # a SEPARATE database session, as on another instance
            return await WRITERS[writer](session, cid, rec)

    task = asyncio.create_task(_write())
    await asyncio.wait_for(paused.guard_passed.wait(), 10)
    # Deletion from another session while the writer sits between its check and its commit:
    # it must NOT be reported complete (the writer holds the shared lock) and must write nothing.
    async with SessionLocal() as session:
        blocked = await delete_conversation(session, cid)
    assert blocked.completed is False and blocked.state == "conflict" and await _tombstones(cid) == 0
    paused.release.set()
    await task  # the writer commits its (old) data ...
    async with SessionLocal() as session:
        done = await delete_conversation(session, cid)  # ... and deletion, once it can run, removes it
    assert done.completed
    assert set((await _counts(cid)).values()) == {0}
    dump = await _database_dump()
    for marker in MARKERS:
        assert marker not in dump, marker
    # And a writer that starts AFTER completion is refused outright (the draft writer's record was
    # disposable and is simply gone, so it has nothing to write to).
    async with SessionLocal() as session:
        if writer == "draft":
            assert await WRITERS[writer](session, cid, rec) is None
        else:
            with pytest.raises(ConversationDeleted):
                await WRITERS[writer](session, cid, rec)


async def test_two_writers_in_two_sessions_both_hold_the_shared_lock_and_deletion_waits_for_both(world, monkeypatch):
    mine = await world.conversation()
    cid = mine["conversationId"]
    a_in, b_in, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def _writer(flag):
        async with SessionLocal() as session:
            await data_lifecycle.ensure_conversation_writable(session, cid)
            flag.set()
            await release.wait()
            session.add(Message(id=new_id(), conversationId=cid, role="assistant", content="late " + NAME, createdAt=utcnow()))
            await session.commit()

    tasks = [asyncio.create_task(_writer(a_in)), asyncio.create_task(_writer(b_in))]
    await asyncio.gather(a_in.wait(), b_in.wait())  # two shared holders at once: fine for writers ...
    async with SessionLocal() as session:
        assert (await delete_conversation(session, cid)).state == "conflict"  # ... exclusive cannot be taken
    release.set()
    await asyncio.gather(*tasks)
    async with SessionLocal() as session:
        assert (await delete_conversation(session, cid)).completed
    assert (await _counts(cid))["messages"] == 0


async def test_unrelated_conversations_are_independent_under_the_guard(world, monkeypatch):
    a = await world.conversation()
    b = await world.conversation()
    paused = PausedWriter(monkeypatch)

    async def _write_a():
        async with SessionLocal() as session:
            await save_message(session, a["conversationId"], "assistant", "hello")

    task = asyncio.create_task(_write_a())
    await asyncio.wait_for(paused.guard_passed.wait(), 10)
    async with SessionLocal() as session:
        assert (await delete_conversation(session, b["conversationId"])).completed  # B is not blocked by A's writer
    paused.release.set()
    await task
    assert (await _counts(a["conversationId"]))["conversation"] == 1


def test_every_writer_takes_the_shared_lock_before_its_write_and_deletion_takes_the_exclusive_one():
    import inspect

    guard = inspect.getsource(data_lifecycle.ensure_conversation_writable)
    assert "pg_advisory_xact_lock_shared" in guard and guard.index("pg_advisory_xact_lock_shared") < guard.index("is_conversation_deleted")
    deletion = inspect.getsource(data_lifecycle.delete_conversation)
    assert "pg_try_advisory_xact_lock" in deletion and deletion.index("pg_try_advisory_xact_lock") < deletion.index("_purge(")
    for module_name, function in (("app.services.conversation", "save_message"), ("app.services.conversation", "save_user_message_with_classification"), ("app.services.conversation", "create_or_update_conversation"),
                                  ("app.services.customer_profile", "_upsert_profile"), ("app.services.recommendation_confirmation", "save_recommendation"),
                                  ("app.services.recommendation_confirmation", "mark_recommendation_draft"), ("app.services.build_capability", "issue_build_token")):
        source = inspect.getsource(getattr(__import__(module_name, fromlist=[function]), function))
        assert "ensure_conversation_writable" in source, f"{module_name}.{function} does not apply rule W"


# ===========================================================================
# 5. Tombstone expiry does not reopen anything
# ===========================================================================

async def test_after_the_tombstone_expires_nothing_can_recreate_the_conversation(world, monkeypatch):
    mine = await world.conversation()
    cid, old_token = mine["conversationId"], mine["conversationToken"]
    kept = await world.recommendation(cid, build_status="saved", product_id="gid://shopify/Product/802")
    async with SessionLocal() as session:
        build_token = await issue_build_token(session, recommendation_id=kept, conversation_id=cid, shop=SHOP)
        assert (await delete_conversation(session, cid)).completed
    conversation_flow._CONVERSATIONS[cid] = [{"role": "user", "content": MESSAGE}]  # a stale object on another instance

    # The clock advances past the tombstone window and retention removes the tombstone.
    monkeypatch.setattr(settings, "retention_execution_enabled", True)
    later = utcnow() + timedelta(days=settings.retention_tombstone_days + 1)
    async with SessionLocal() as session:
        report = await run_retention(session, execute=True, now=later)
    assert report.deleted.get("tombstones", 0) >= 1 and await _tombstones(cid) == 0

    with TestClient(app) as client:
        headers = {CONVERSATION_TOKEN_HEADER: old_token}
        assert client.get("/chat", params={"history": "true", "conversation_id": cid}, headers=headers).status_code == 401
        assert client.post("/chat", json={"conversation_id": cid, "message": "I love vanilla"}, headers=headers).status_code == 401
        assert client.post("/chat/delete", json={"conversation_id": cid}, headers=headers).status_code == 401
        assert client.post("/api/save-build", json={"recommendationId": kept, "buildToken": build_token, "ratios": RATIOS}).status_code == 403
        internal = {"X-Internal-Api-Key": settings.internal_api_key}
        assert client.get("/internal/chat/history", params={"conversation_id": cid}, headers=internal).json() == {"messages": []}
        continued = client.post("/internal/chat", json={"conversation_id": cid, "message": "I love vanilla and sandalwood"}, headers=internal)
        new_id_event = next(json.loads(line[6:]) for line in continued.text.splitlines() if line.startswith("data: ") and '"id"' in line)
        assert new_id_event["conversation_id"] != cid  # a fresh, server-minted conversation, never the old id
        world.conversation_ids.append(new_id_event["conversation_id"])
        fresh = client.post("/chat/session", json={})  # new sessions still work and are server-controlled
        assert fresh.status_code == 200 and fresh.json()["conversationId"] != cid
        world.conversation_ids.append(fresh.json()["conversationId"])
    # Direct service writes with the old id: the guard no longer knows it, but every writer that
    # could be reached for it requires a row or a capability that no longer exists ...
    async with SessionLocal() as session:
        with pytest.raises(ConversationDeleted):
            await mark_recommendation_draft(session, kept, name=BLEND_NAME, ratios=RATIOS)  # detached: refused for ever
        with pytest.raises(ConversationDeleted):
            await execute_build_commerce(session, SHOP, recommendation_id=kept, ratios=RATIOS, name=BLEND_NAME)
    assert set((await _counts(cid)).values()) == {0}
    dump = await _database_dump()
    for marker in MARKERS:
        assert marker not in dump, marker


def test_no_route_can_adopt_a_caller_supplied_conversation_id_for_a_missing_row():
    """The only routes that take a conversation id either require a live capability (public) or
    verify the row exists and otherwise mint a fresh id (internal). Neither path calls the
    insert-capable upsert for an id the server did not mint."""
    import inspect

    from app.api import chat as chat_module

    public = inspect.getsource(chat_module._resolve_public_conversation)
    internal = inspect.getsource(chat_module._resolve_internal_conversation)
    assert "authorize_conversation" in public and "create_conversation_with_capability" in public
    assert "select(Conversation.id)" in internal and "is_conversation_deleted" in internal and "create_conversation_with_capability" in internal
    assert "create_or_update_conversation" not in public and "create_or_update_conversation" not in internal


# ===========================================================================
# 6. Deletion around in-flight commerce
# ===========================================================================

async def _build_ready(world):
    mine = await world.conversation()
    cid = mine["conversationId"]
    rec = await world.recommendation(cid)
    async with SessionLocal() as session:
        row = await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == rec))
        row.productsJson = [{"title": "Alpha", "notes": ["Rose"]}, {"title": "Beta", "notes": ["Amber"]}]
        row.ratiosJson = [{"productTitle": "Alpha", "ratioPercent": 60}, {"productTitle": "Beta", "ratioPercent": 40}]
        await session.commit()
    return mine, cid, rec


@pytest.mark.parametrize("outcome", ["remote_success", "ambiguous_timeout", "process_interruption"])
async def test_deletion_during_a_creation_conflicts_then_keeps_only_reconciliation_facts(world, monkeypatch, outcome):
    mine, cid, rec = await _build_ready(world)
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec)
    inside, release = asyncio.Event(), asyncio.Event()

    class _Crash(BaseException):
        pass

    async def _create(*a, **kw):
        shopify.calls.append("create_product")
        inside.set()
        await release.wait()
        if outcome == "ambiguous_timeout":
            raise TimeoutError("timed out")
        if outcome == "process_interruption":
            raise _Crash()
        return {"id": shopify.product_id, "handle": "custom-blend"}

    monkeypatch.setattr(builds, "create_product", _create)

    async def _operation():
        async with SessionLocal() as session:
            return await execute_build_commerce(session, SHOP, recommendation_id=rec, ratios=RATIOS, name=BLEND_NAME, save_draft=True)

    task = asyncio.create_task(_operation())
    await asyncio.wait_for(inside.wait(), 10)               # Shopify request in flight; build lock held
    async with SessionLocal() as session:
        assert (await delete_conversation(session, cid)).state == "conflict"  # deletion cannot unsend it: it waits
    release.set()
    if outcome == "remote_success":
        assert (await task)["created"] is True
    else:
        with pytest.raises((TimeoutError, builds.BuildWriteAmbiguous, _Crash)):
            await task
    async with SessionLocal() as session:
        result = await delete_conversation(session, cid)   # now it can run: the record is MINIMIZED, not removed
        assert result.completed and result.minimized_commerce_records == 1
        row = await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == rec))
    expected_status = {"remote_success": "saved", "ambiguous_timeout": "pending_review", "process_interruption": "creating"}[outcome]
    assert row.buildStatus == expected_status and row.conversationId.startswith("deleted-")
    assert (row.shopifyProductId == shopify.product_id) is (outcome == "remote_success")  # the reconciliation fact survives
    assert row.draftName is None and row.customerFacingJson is None and row.customerProfileJson == {}  # the personal data does not
    assert shopify.calls.count("create_product") == 1
    async with SessionLocal() as session:
        with pytest.raises((ConversationDeleted, BuildPendingReview)):  # no duplicate creation, ever
            await execute_build_commerce(session, SHOP, recommendation_id=rec, ratios=RATIOS, name=BLEND_NAME)
    assert shopify.calls.count("create_product") == 1
    dump = await _database_dump()
    for marker in MARKERS:
        assert marker not in dump, marker


async def test_known_product_id_before_deletion_and_deletion_while_repricing(world, monkeypatch):
    mine, cid, rec = await _build_ready(world)
    async with SessionLocal() as session:
        row = await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == rec))
        row.shopifyProductId, row.buildStatus = "gid://shopify/Product/803", "saved"
        await session.commit()
    shopify = ShopifyWrites(monkeypatch, recommendation_id=rec, product_id="gid://shopify/Product/803")
    inside, release = asyncio.Event(), asyncio.Event()

    async def _slow_variant(*a, **kw):
        shopify.calls.append("create_variant")
        inside.set()
        await release.wait()
        return {"id": "gid://shopify/ProductVariant/9", "price": "60.00"}

    monkeypatch.setattr(builds, "create_variant", _slow_variant)

    async def _reprice():
        async with SessionLocal() as session:
            return await execute_build_commerce(session, SHOP, recommendation_id=rec, ratios={"top": 10, "middle": 10, "base": 80}, name=None)

    task = asyncio.create_task(_reprice())
    await asyncio.wait_for(inside.wait(), 10)
    async with SessionLocal() as session:
        assert (await delete_conversation(session, cid)).state == "conflict"
    release.set()
    assert (await task)["variantId"] == "gid://shopify/ProductVariant/9"
    async with SessionLocal() as session:
        assert (await delete_conversation(session, cid)).completed
        row = await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == rec))
    assert row.shopifyProductId == "gid://shopify/Product/803" and row.shopifyVariantId == "gid://shopify/ProductVariant/9" and row.buildStatus == "saved"
    assert row.draftRatiosJson is None and row.draftName is None


async def test_deletion_triggers_no_external_operation(world, monkeypatch, network_guard):
    from app.integrations import odoo_client
    from app.shopify import admin_client

    async def _never(*a, **kw):
        raise AssertionError("deletion must not call an external service")

    monkeypatch.setattr(admin_client, "admin_graphql", _never)
    monkeypatch.setattr(odoo_client, "get_inventory_by_skus", _never)
    monkeypatch.setattr(conversation_flow, "call_openai_once", _never)
    mine = await world.conversation()
    await world.recommendation(mine["conversationId"], build_status="pending_review", product_id="gid://shopify/Product/804")
    attempts = len(network_guard.attempts)
    with TestClient(app) as client:
        assert _delete(client, mine).status_code == 200
    assert len(network_guard.attempts) == attempts


# ===========================================================================
# 7. Status cannot be read by id; retention is separate
# ===========================================================================

async def test_no_status_endpoint_exists_and_the_old_token_reveals_nothing_after_completion(world):
    paths = " ".join(app.openapi()["paths"]).lower()
    assert "status" not in paths.replace("healthz", "") or "/chat/delete" in paths
    assert not [p for p in app.openapi()["paths"] if "delet" in p and p != "/chat/delete"]
    mine = await world.conversation()
    other = await world.conversation()
    with TestClient(app) as client:
        assert _delete(client, mine).status_code == 200
        own_after = _delete(client, mine)
        strangers = client.post("/chat/delete", json={"conversation_id": mine["conversationId"]}, headers={CONVERSATION_TOKEN_HEADER: other["conversationToken"]})
        assert own_after.status_code == strangers.status_code == 401 and own_after.json() == strangers.json()


async def test_retention_conflicts_are_held_and_customer_deletion_never_reads_the_retention_flag(world, monkeypatch):
    from app.services.turn_lock import conversation_turn_lock

    monkeypatch.setattr(settings, "retention_execution_enabled", True)
    old = await world.conversation(age_days=300, with_data=False)
    async with conversation_turn_lock(old["conversationId"]):
        async with SessionLocal() as session:
            report = await run_retention(session, execute=True)
    assert report.held.get("operationInFlight") == 1 and report.deleted.get("conversations") is None
    import inspect

    assert "retention_execution_enabled" not in inspect.getsource(data_lifecycle.delete_conversation)
    assert "retention_execution_enabled" not in inspect.getsource(__import__("app.api.chat", fromlist=["x"]).delete_conversation_route)


# ===========================================================================
# 8. Phase 7 carryovers
# ===========================================================================

async def test_enabling_retention_alone_cannot_bypass_the_shared_data_review(world, monkeypatch):
    """Approval to RUN age-based retention is not evidence that deleting the shared rows is safe.
    The gate sits at the common destructive boundary, so every caller hits it."""
    from app.services.data_lifecycle import SharedDataReviewPending

    monkeypatch.setattr(settings, "shared_data_deletion_reviewed", False)
    monkeypatch.setattr(settings, "retention_execution_enabled", True)
    old = await world.conversation(age_days=300)
    before = await _counts(old["conversationId"])
    async with SessionLocal() as session:
        report = await run_retention(session, execute=True)
        assert report.dry_run is True and report.deleted == {} and report.held.get("sharedDataReviewPending") == 1
        assert report.eligible.get("inactiveConversations", 0) >= 1  # it still reports what it WOULD do
        with pytest.raises(SharedDataReviewPending):  # a direct service caller is refused the same way
            await delete_conversation(session, old["conversationId"], origin="retention")
    assert await _counts(old["conversationId"]) == before and await _tombstones(old["conversationId"]) == 0
    monkeypatch.setattr(settings, "shared_data_deletion_reviewed", True)
    async with SessionLocal() as session:
        assert (await run_retention(session, execute=True)).deleted.get("conversations", 0) >= 1


def test_a_401_is_never_documented_as_proof_of_deletion():
    for path in ("docs/CHAT_SECURITY_CONTRACT.md", "docs/DATA_RETENTION_AND_DELETION.md"):
        text_ = " ".join(open(path).read().split())
        assert "not proof of deletion" in text_, path
        assert "means the deletion had completed" not in text_ and "means the deletion completed" not in text_, path
