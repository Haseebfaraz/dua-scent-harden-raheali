"""Account linking and My Builds (App Proxy). Ownership comes only from Shopify's signed
logged_in_customer_id plus the conversation capability; never from an email, a name or an id the
browser chooses. Database-backed; Shopify is a fake."""

import hashlib
import hmac as _hmac
import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.api import storefront
from app.config import settings
from app.db.models import BuildCapability, Conversation, ConversationCapability, CustomerProfileState, FragranceRecommendation
from app.db.session import SessionLocal
from app.db.time import utcnow
from app.main import app
from app.services.conversation_capability import create_conversation_with_capability
from app.services.customer_profile import get_customer_profile, save_customer_profile_fields

SECRET = "storefront-test-secret"
SHOP = "test-shop.myshopify.com"


def _proxy(customer_id: str = "", **extra) -> dict:
    params = {"shop": SHOP, "timestamp": "1", "path_prefix": "/apps/scent-library", "logged_in_customer_id": customer_id, **extra}
    message = "".join(f"{k}={v}" for k, v in sorted(params.items()))
    return {**params, "signature": _hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(settings, "shopify_api_secret", SECRET)
    monkeypatch.setattr(settings, "shopify_shop_domain", SHOP)


@pytest.fixture
def shopify(monkeypatch):
    """Fake Admin API: customer contact lookups and product status reads."""
    state = {"contact": {"firstName": "Maya", "email": "maya@example.com"}, "fail": False, "calls": []}

    async def _contact(session, shop, customer_id):
        state["calls"].append(("contact", customer_id))
        return None if state["fail"] else state["contact"]

    async def _graphql(session, shop, query, variables=None):
        state["calls"].append(("graphql", variables))
        return {"data": {"nodes": [{"id": pid, "handle": "velvet-bloom", "status": "ACTIVE"} for pid in variables["ids"]]}}

    monkeypatch.setattr(storefront, "fetch_customer_contact", _contact)
    monkeypatch.setattr(storefront, "admin_graphql", _graphql)
    return state


async def _conversation() -> tuple[str, str]:
    async with SessionLocal() as session:
        conversation_id, token, _ = await create_conversation_with_capability(session)
    return conversation_id, token


async def _recommendation(conversation_id: str, *, status="confirmed", build_status="saved") -> str:
    rec_id = f"pytest-sf-{uuid.uuid4().hex[:10]}"
    async with SessionLocal() as session:
        session.add(FragranceRecommendation(
            id=rec_id, conversationId=conversation_id, customerProfileJson={}, productsJson=[{"title": "Secret Source Product", "notes": ["Rose"]}],
            combinationType="HYBRID", scoreJson={}, evidenceJson={}, ratiosJson=[], customerFacingJson={"customerFacingName": "Velvet Bloom"},
            status=status, buildStatus=build_status, shopifyProductId="gid://shopify/Product/9" if build_status == "saved" else None, createdAt=utcnow(),
        ))
        await session.commit()
    return rec_id


async def _cleanup(*conversation_ids):
    async with SessionLocal() as session:
        await session.execute(delete(FragranceRecommendation).where(FragranceRecommendation.conversationId.in_(conversation_ids)))
        await session.execute(delete(CustomerProfileState).where(CustomerProfileState.conversationId.in_(conversation_ids)))
        await session.execute(delete(Conversation).where(Conversation.id.in_(conversation_ids)))
        await session.commit()


def _link(client, customer_id, conversation_id, token):
    return client.post("/apps/scent-library/account/link", params=_proxy(customer_id), json={"conversationId": conversation_id, "conversationToken": token})


# ---------------------------------------------------------------- account link

async def test_guest_link_is_a_no_op(shopify):
    conversation_id, token = await _conversation()
    try:
        with TestClient(app) as client:
            response = _link(client, "", conversation_id, token)
        assert response.json() == {"linked": False, "signedIn": False}
        assert shopify["calls"] == []
    finally:
        await _cleanup(conversation_id)


async def test_signed_customer_binds_conversation_and_fills_empty_account_details(shopify):
    conversation_id, token = await _conversation()
    try:
        with TestClient(app) as client:
            response = _link(client, "1001", conversation_id, token)
        assert response.status_code == 200 and response.json()["nameKnown"] and response.json()["emailKnown"]
        async with SessionLocal() as session:
            profile = await get_customer_profile(session, conversation_id)
            capability = await session.scalar(select(ConversationCapability).where(ConversationCapability.conversationId == conversation_id))
            conversation = await session.get(Conversation, conversation_id)
        assert (profile["name"], profile["email"]) == ("Maya", "maya@example.com")
        assert (conversation.customerName, conversation.customerEmail) == ("Maya", "maya@example.com")
        assert capability.verifiedShopifyCustomerId == "1001"
    finally:
        await _cleanup(conversation_id)


async def test_existing_profile_values_are_never_overwritten(shopify):
    conversation_id, token = await _conversation()
    async with SessionLocal() as session:
        await save_customer_profile_fields(session, conversation_id, {"name": "Mo"})
    try:
        with TestClient(app) as client:
            _link(client, "1001", conversation_id, token)
        async with SessionLocal() as session:
            profile = await get_customer_profile(session, conversation_id)
        assert profile["name"] == "Mo" and profile["email"] == "maya@example.com"
    finally:
        await _cleanup(conversation_id)


async def test_account_without_a_name_leaves_the_name_to_the_existing_chat_flow(shopify):
    shopify["contact"] = {"firstName": None, "email": "noname@example.com"}
    conversation_id, token = await _conversation()
    try:
        with TestClient(app) as client:
            body = _link(client, "1002", conversation_id, token).json()
        assert body["nameKnown"] is False and body["emailKnown"] is True
    finally:
        await _cleanup(conversation_id)


async def test_conversation_bound_to_one_customer_is_refused_for_another(shopify):
    conversation_id, token = await _conversation()
    try:
        with TestClient(app) as client:
            assert _link(client, "1001", conversation_id, token).status_code == 200
            switched = _link(client, "2002", conversation_id, token)
        assert switched.status_code == 401 and switched.json()["code"] == "conversation_not_authorized"
        assert ("contact", "2002") not in shopify["calls"]
    finally:
        await _cleanup(conversation_id)


async def test_conversation_id_without_its_token_links_nothing(shopify):
    conversation_id, _token = await _conversation()
    other_id, other_token = await _conversation()
    try:
        with TestClient(app) as client:
            assert _link(client, "1001", conversation_id, "guessed-token").status_code == 401
            assert _link(client, "1001", conversation_id, other_token).status_code == 401  # a token for another conversation
            assert _link(client, "1001", conversation_id, None).status_code == 401
        assert shopify["calls"] == []
    finally:
        await _cleanup(conversation_id, other_id)


async def test_unsigned_or_forged_requests_are_rejected(shopify):
    conversation_id, token = await _conversation()
    try:
        with TestClient(app) as client:
            forged = _proxy("1001")
            forged["logged_in_customer_id"] = "9999"  # signature no longer matches
            response = client.post("/apps/scent-library/account/link", params=forged, json={"conversationId": conversation_id, "conversationToken": token})
            unsigned = client.post("/apps/scent-library/account/link", params={"shop": SHOP, "logged_in_customer_id": "1001"}, json={})
        assert response.status_code == 400 and unsigned.status_code == 400
    finally:
        await _cleanup(conversation_id)


async def test_contact_lookup_failure_still_links_without_inventing_details(shopify):
    shopify["fail"] = True
    conversation_id, token = await _conversation()
    try:
        with TestClient(app) as client:
            body = _link(client, "1001", conversation_id, token).json()
        assert body["linked"] is True and body["nameKnown"] is False and body["emailKnown"] is False
    finally:
        await _cleanup(conversation_id)


async def test_account_link_logs_no_token_or_contact_details(shopify, caplog):
    conversation_id, token = await _conversation()
    try:
        with caplog.at_level(logging.INFO):
            with TestClient(app) as client:
                _link(client, "1001", conversation_id, token)
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "ACCOUNT_LINKED" in logged
        for value in (token, "maya@example.com", "Maya"):
            assert value not in logged
    finally:
        await _cleanup(conversation_id)


# ---------------------------------------------------------------- My Builds

async def test_my_builds_lists_only_the_signed_customers_confirmed_builds(shopify):
    mine, my_token = await _conversation()
    theirs, their_token = await _conversation()
    try:
        with TestClient(app) as client:
            _link(client, "1001", mine, my_token)
            _link(client, "2002", theirs, their_token)
        saved = await _recommendation(mine)
        draft = await _recommendation(mine, build_status="draft")
        await _recommendation(mine, status="pending", build_status="draft")  # never previewed: not listed
        other = await _recommendation(theirs)
        with TestClient(app) as client:
            body = client.get("/apps/scent-library/my-builds", params=_proxy("1001")).json()
            guest = client.get("/apps/scent-library/my-builds", params=_proxy(""))
        ids = {b["recommendationId"] for b in body["builds"]}
        assert ids == {saved, draft} and other not in ids and body["total"] == 2
        listed = {b["recommendationId"]: b for b in body["builds"]}
        assert listed[saved]["productUrl"] == f"https://{SHOP}/products/velvet-bloom" and listed[saved]["status"] == "saved"
        assert listed[draft]["productUrl"] is None and listed[draft]["status"] == "draft"
        assert "Secret Source Product" not in str(body)  # customer-facing names only
        assert guest.status_code == 401 and guest.json()["code"] == "sign_in_required"
    finally:
        await _cleanup(mine, theirs)


async def test_reopening_a_build_mints_a_capability_bound_to_the_owner(shopify):
    mine, my_token = await _conversation()
    theirs, their_token = await _conversation()
    try:
        with TestClient(app) as client:
            _link(client, "1001", mine, my_token)
            _link(client, "2002", theirs, their_token)
        rec = await _recommendation(mine, build_status="draft")
        other = await _recommendation(theirs, build_status="draft")
        with TestClient(app) as client:
            opened = client.post("/apps/scent-library/my-builds/open", params=_proxy("1001"), json={"recommendationId": rec})
            stolen = client.post("/apps/scent-library/my-builds/open", params=_proxy("1001"), json={"recommendationId": other})
            unknown = client.post("/apps/scent-library/my-builds/open", params=_proxy("1001"), json={"recommendationId": "nope"})
        assert opened.status_code == 200 and opened.json()["previewUrl"].startswith(f"https://{SHOP}/apps/scent-library/fragrance-preview?recommendationId={rec}&bt=")
        assert stolen.status_code == 404 and unknown.status_code == 404 and stolen.json() == unknown.json()
        async with SessionLocal() as session:
            caps = (await session.scalars(select(BuildCapability).where(BuildCapability.recommendationId == rec))).all()
            assert [c.verifiedShopifyCustomerId for c in caps] == ["1001"]
            assert not (await session.scalars(select(BuildCapability).where(BuildCapability.recommendationId == other))).all()
    finally:
        await _cleanup(mine, theirs)
