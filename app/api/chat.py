"""Chat endpoints.

Two surfaces share the orchestration but not the trust model:

  * PUBLIC (`/chat/session`, `/chat`, `GET /chat`): called directly by the storefront browser.
    Phase 2 (security, F6 / F7 / F8 / N2) made this a real trust boundary:
      - a conversation is created only by the server (`POST /chat/session`, or implicitly by a
        first `POST /chat` with no conversation id). The response carries the conversation id
        AND a server-minted conversation capability token; only the token's hash is stored.
      - every later read or write of that conversation must present the token
        (`X-Conversation-Token` header, or `conversation_token` in the chat body). A conversation
        id alone -- or an email, a name, a shop, an Origin -- authorizes nothing.
      - name / email in the body are SELF-REPORTED contact data: validated for shape, used only
        to fill an empty profile field, never as authentication.
      - the browser can no longer inject assistant text (`greeting` is ignored); a welcome line
        is server-owned.
      - hard input limits, PostgreSQL-backed rate limits, a per-conversation turn lock, a
        per-process concurrency cap, and a turn deadline all run BEFORE any OpenAI or tool work.
      - SSE events reaching the browser are allowlisted field-by-field.

  * INTERNAL (`/internal/chat`, `/internal/chat/history`): the retiring Node adapter, server to
    server, authenticated by `X-Internal-Api-Key` (now mandatory: unset means refused, not
    skipped). Node resolves the customer's Shopify session itself, so this trusted channel keeps
    id-based conversation addressing; it still gets the same input limits, per-conversation
    limits, lock, and context bounds.
"""

import asyncio
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.conversation_flow import call_ai, deterministic_scope_reply, get_conversation, set_conversation_cache
from app.ai.scope_responses import unresolved_reply
from app.ai.security_gate import classify_message, permissions_for
from app.api.client_identity import client_ip
from app.api.request_limits import InvalidChatInput, validate_chat_message, validate_conversation_id, validate_token_shape
from app.config import settings
from app.db.models import Conversation
from app.db.session import get_session
from app.schemas.chat import ChatRequest, ChatSessionRequest
from app.services.build_capability import preview_url_for_logging
from app.services.conversation import create_or_update_conversation, save_message, save_user_message_with_classification
from app.services.conversation_capability import (
    CONVERSATION_TOKEN_HEADER,
    ConversationNotAuthorized,
    authorize_conversation,
    create_conversation_with_capability,
)
from app.services.customer_identity import SelfReportedIdentity, self_reported_identity
from app.services.customer_profile import get_customer_profile, save_customer_profile_field
from app.services.legacy_preview_recovery import resolve_legacy_preview_short_circuit
from app.services.rate_limit import Limit, RateLimitUnavailable, RateLimited, enforce, hash_abuse_identity, limit
from app.services.turn_lock import TurnInProgress, conversation_turn_lock
from app.shopify.trusted_shop import TrustedShopNotConfigured, trusted_shop

logger = logging.getLogger(__name__)
router = APIRouter()

NO_STORE_HEADERS = {"Cache-Control": "no-store"}
_NOT_AUTHORIZED_BODY = {"error": "This conversation session is not valid or has expired. Please start a new conversation.", "code": "conversation_not_authorized"}


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def require_internal_api_key(x_internal_api_key: str | None = Header(default=None)) -> None:
    """Server-to-server shared key for the Node adapter. Phase 2: fail closed when unset."""
    configured = settings.internal_api_key
    if not configured or not x_internal_api_key or not hmac.compare_digest(x_internal_api_key.encode(), configured.encode()):
        raise HTTPException(status_code=401, detail="invalid or missing internal API key")


async def _enforce_limits(session: AsyncSession, limits: list[Limit]) -> None:
    try:
        await enforce(session, limits)
    except RateLimited as err:
        raise HTTPException(status_code=429, detail="Too many requests. Please wait a moment and try again.", headers={"Retry-After": str(err.retry_after_seconds), **NO_STORE_HEADERS}) from None
    except RateLimitUnavailable:
        raise HTTPException(status_code=503, detail="Service temporarily unavailable.", headers=NO_STORE_HEADERS) from None


class _TurnSlots:
    """Per-process cap on simultaneous model-bearing turns. Non-blocking: a burst beyond the cap
    is refused with 503 + Retry-After rather than queued, so a flood cannot pile up work."""

    def __init__(self) -> None:
        self.active = 0

    def acquire(self) -> bool:
        if self.active >= settings.chat_max_concurrent_turns:
            return False
        self.active += 1
        return True

    def release(self) -> None:
        self.active = max(0, self.active - 1)


turn_slots = _TurnSlots()


def _conversation_token_from(request: Request, body_token: str | None) -> str | None:
    return request.headers.get(CONVERSATION_TOKEN_HEADER) or body_token


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------

def _sse_line(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


# Allowlist of what each event type may carry to the browser. Anything else (full profile
# dumps, candidate scores/counts, internal readiness text, unknown event types) is dropped.
_PUBLIC_EVENT_FIELDS: dict[str, set[str]] = {
    "id": {"conversation_id", "conversation_token", "expires_at"},
    "chunk": {"chunk"},
    "message_complete": set(),
    "end_turn": set(),
    "error": {"error"},
    "preview_ready": {"recommendationId", "previewId", "previewUrl"},
    "profile_progress": set(),
    "analysis_progress": set(),
    "candidate_products": set(),
    "combination_recommendations": set(),
    "recommendation_selected": {"recommendationId"},
}


def public_sse_event(event: dict) -> dict | None:
    event_type = event.get("type")
    allowed = _PUBLIC_EVENT_FIELDS.get(event_type)
    if allowed is None:
        return None
    return {"type": event_type, **{k: v for k, v in event.items() if k in allowed}}


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

async def _history_payload(session: AsyncSession, conversation_id: str | None) -> dict:
    history = (await get_conversation(session, conversation_id))["history"] if conversation_id else []

    if conversation_id:
        profile = await get_customer_profile(session, conversation_id)
        if profile.get("pendingRecreateRecommendationId"):
            ask_text = "What would you like to change about your fragrance?"
            history.append({"role": "assistant", "content": ask_text})
            set_conversation_cache(conversation_id, history)
            try:
                await save_message(session, conversation_id, "assistant", ask_text)
            except Exception as err:
                logger.error("Failed to persist recreate re-entry message: %s", type(err).__name__)
            await save_customer_profile_field(session, conversation_id, "pendingRecreateRecommendationId", None)

    visible = [
        {"role": m["role"], "content": m["content"]}
        for m in history
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m["content"].strip()
    ]
    # Bounded: the most recent customer-visible messages only; tool/system turns never appear.
    return {"messages": visible[-settings.chat_history_max_messages:]}


@router.get("/internal/chat/history", dependencies=[Depends(require_internal_api_key)])
async def chat_history(conversation_id: str | None = None, session: AsyncSession = Depends(get_session)) -> JSONResponse:
    if conversation_id is not None:
        try:
            validate_conversation_id(conversation_id)
        except InvalidChatInput:
            return JSONResponse({"messages": []}, headers=NO_STORE_HEADERS)
    return JSONResponse(await _history_payload(session, conversation_id), headers=NO_STORE_HEADERS)


@router.get("/chat")
async def public_chat_history(request: Request, history: str | None = None, conversation_id: str | None = None, session: AsyncSession = Depends(get_session)) -> JSONResponse:
    # Matches the widget's fetchChatHistory() call shape: only ?history=true returns history.
    if history != "true" or not conversation_id:
        return JSONResponse({"messages": []}, headers=NO_STORE_HEADERS)
    try:
        validate_conversation_id(conversation_id)
        token = validate_token_shape(request.headers.get(CONVERSATION_TOKEN_HEADER))
    except InvalidChatInput:
        return JSONResponse(_NOT_AUTHORIZED_BODY, status_code=401, headers=NO_STORE_HEADERS)
    try:
        await authorize_conversation(session, token=token, conversation_id=conversation_id)
    except ConversationNotAuthorized:
        return JSONResponse(_NOT_AUTHORIZED_BODY, status_code=401, headers=NO_STORE_HEADERS)
    ip_subject = hash_abuse_identity(client_ip(request))
    await _enforce_limits(session, [
        limit("history_read_conv", conversation_id, settings.rate_limit_history_read_per_conversation),
        limit("history_read_ip", ip_subject, settings.rate_limit_history_read_per_ip),
    ])
    return JSONResponse(await _history_payload(session, conversation_id), headers=NO_STORE_HEADERS)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

async def _seed_welcome(session: AsyncSession, conversation_id: str) -> str:
    welcome = settings.chat_welcome_message
    await save_message(session, conversation_id, "assistant", welcome)
    set_conversation_cache(conversation_id, [{"role": "assistant", "content": welcome}])
    return welcome


@router.post("/chat/session")
async def create_chat_session(request: Request, body: ChatSessionRequest | None = None, session: AsyncSession = Depends(get_session)) -> JSONResponse:
    """Server-controlled conversation bootstrap for the storefront. The caller chooses nothing."""
    body = body or ChatSessionRequest()
    ip_subject = hash_abuse_identity(client_ip(request))
    await _enforce_limits(session, [limit("conversation_create_ip", ip_subject, settings.rate_limit_conversation_create_per_ip)])
    conversation_id, token, capability = await create_conversation_with_capability(session)
    welcome = await _seed_welcome(session, conversation_id) if body.with_welcome else None
    logger.info("CONVERSATION_CREATED %s", json.dumps({"conversationId": conversation_id}))
    return JSONResponse(
        {"conversationId": conversation_id, "conversationToken": token, "expiresAt": capability.expiresAt.isoformat() + "Z", "welcomeMessage": welcome},
        headers=NO_STORE_HEADERS,
    )


# ---------------------------------------------------------------------------
# Chat turn
# ---------------------------------------------------------------------------

async def _resolve_public_conversation(request: Request, body: ChatRequest, session: AsyncSession, ip_subject: str) -> tuple[str, str | None, str | None]:
    """Returns (conversation_id, new_token_or_None, expires_at_or_None). Raises HTTPException."""
    if body.conversation_id is None:
        # Implicit bootstrap on the first message: same limits and the same server-minted pair
        # as /chat/session. The token is delivered once, in the stream's `id` event.
        await _enforce_limits(session, [
            limit("conversation_create_ip", ip_subject, settings.rate_limit_conversation_create_per_ip),
            limit("chat_turn_ip", ip_subject, settings.rate_limit_chat_turn_per_ip),
        ])
        conversation_id, token, capability = await create_conversation_with_capability(session)
        if body.with_welcome:
            await _seed_welcome(session, conversation_id)
        logger.info("CONVERSATION_CREATED %s", json.dumps({"conversationId": conversation_id}))
        return conversation_id, token, capability.expiresAt.isoformat() + "Z"

    try:
        conversation_id = validate_conversation_id(body.conversation_id)
        token = validate_token_shape(_conversation_token_from(request, body.conversation_token))
    except InvalidChatInput:
        raise HTTPException(status_code=401, detail=_NOT_AUTHORIZED_BODY["error"], headers=NO_STORE_HEADERS) from None
    try:
        await authorize_conversation(session, token=token, conversation_id=conversation_id, touch=True)
    except ConversationNotAuthorized:
        raise HTTPException(status_code=401, detail=_NOT_AUTHORIZED_BODY["error"], headers=NO_STORE_HEADERS) from None
    await _enforce_limits(session, [
        limit("chat_turn_conv", conversation_id, settings.rate_limit_chat_turn_per_conversation),
        limit("chat_turn_conv_day", conversation_id, settings.rate_limit_chat_turn_per_conversation_daily),
        limit("chat_turn_ip", ip_subject, settings.rate_limit_chat_turn_per_ip),
    ])
    return conversation_id, None, None


async def _resolve_internal_conversation(body: ChatRequest, session: AsyncSession) -> str:
    """Trusted Node adapter: continue an existing conversation by id, else mint a new one. The
    caller can never make the server adopt an arbitrary, non-existent id."""
    if body.conversation_id is not None:
        try:
            conversation_id = validate_conversation_id(body.conversation_id)
        except InvalidChatInput:
            conversation_id = None
        if conversation_id and await session.scalar(select(Conversation.id).where(Conversation.id == conversation_id)):
            await _enforce_limits(session, [
                limit("chat_turn_conv", conversation_id, settings.rate_limit_chat_turn_per_conversation),
                limit("chat_turn_conv_day", conversation_id, settings.rate_limit_chat_turn_per_conversation_daily),
            ])
            return conversation_id
    conversation_id, _token, _capability = await create_conversation_with_capability(session)
    return conversation_id


async def _run_chat_turn(
    request: Request, body: ChatRequest, session: AsyncSession, *, public: bool,
) -> StreamingResponse:
    # ---- VALIDATE INPUT (cheap, before anything else) ----
    try:
        user_message = validate_chat_message(body.message)
    except InvalidChatInput as err:
        raise HTTPException(status_code=400, detail=str(err), headers=NO_STORE_HEADERS) from None

    try:
        shop_domain = trusted_shop()
    except TrustedShopNotConfigured:
        raise HTTPException(status_code=503, detail="service not configured", headers=NO_STORE_HEADERS) from None

    # ---- AUTHORIZE + RATE LIMIT (before the lock, before any model work) ----
    ip_subject = hash_abuse_identity(client_ip(request))
    if public:
        conversation_id, new_token, new_token_expires_at = await _resolve_public_conversation(request, body, session, ip_subject)
    else:
        conversation_id = await _resolve_internal_conversation(body, session)
        new_token, new_token_expires_at = None, None

    identity: SelfReportedIdentity = self_reported_identity(body.customer_name, body.customer_email)

    # ---- SCOPE / SECURITY GATE (Phase 4: after auth + limits, before any model work) ----
    # Layer 1 is deterministic; layer 2 is at most one low-privilege structured classifier call
    # with no history, no tools and no private data. The classification is validated against a
    # fixed enum and the SERVER routes on it below; it never selects tools or actions itself.
    # Minimal customer-safe context only: the previous assistant reply (customer-visible text),
    # used to tell a short answer to a pending question from generic small talk. It is read from
    # the already-projected model history; no profile, ids, capabilities or private data.
    prior = (await get_conversation(session, conversation_id))["history"]
    last_assistant_message = next((m.get("content") for m in reversed(prior) if m.get("role") == "assistant" and isinstance(m.get("content"), str)), None)
    gate = await classify_message(user_message, last_assistant_message=last_assistant_message, conversation_has_fragrance_context=bool(prior))
    # Phase 4A: ONE server-owned permissions decision for the whole turn. UNRESOLVED (classifier
    # disabled / unavailable / timed out / malformed) permits nothing: fail closed.
    permissions = permissions_for(gate)
    logger.info("SECURITY_GATE_DECISION %s", json.dumps({
        "conversationId": conversation_id, "classification": gate.classification, "reasonCode": gate.reason_code,
        "version": gate.version, "semanticUsed": gate.semantic_used,
        "route": "model" if permissions.model_completion else "deterministic_reply", "public": public,
    }))
    if gate.classification == "ATTACK_EXTRACTION":
        # Repeated attacks throttle the conversation and the source address for a while. Counting
        # happens here, so the escalation is a pure server decision.
        await _enforce_limits(session, [
            limit("security_denied", conversation_id, settings.rate_limit_security_denied_per_conversation),
            limit("security_denied_ip", ip_subject, settings.rate_limit_security_denied_per_ip),
        ])

    # ---- CONCURRENCY (one turn per conversation across instances; per-process global cap) ----
    lock = conversation_turn_lock(conversation_id)
    try:
        await lock.__aenter__()
    except TurnInProgress as err:
        raise HTTPException(status_code=409, detail=str(err), headers={"Retry-After": "5", **NO_STORE_HEADERS}) from None
    if not turn_slots.acquire():
        await lock.__aexit__(None, None, None)
        raise HTTPException(status_code=503, detail="The fragrance studio is busy right now. Please try again in a moment.", headers={"Retry-After": "10", **NO_STORE_HEADERS})

    async def _stream():
        try:
            try:
                conv = await get_conversation(session, conversation_id)
                history = conv["history"]
                # The model-facing history gets the gated projection (raw attack text is never
                # replayed; a mixed message keeps only its fragrance part). The raw message is
                # still persisted below, alongside its classification.
                history.append({"role": "user", "content": gate.model_history_content(user_message)})

                deterministic_reply = None
                if not permissions.model_completion:
                    deterministic_reply = deterministic_scope_reply(gate, user_message, conversation_id, len(history)) or unresolved_reply(f"{conversation_id}:{len(history)}")
                legacy_short_circuit = None
                if permissions.legacy_recovery:
                    # Legacy recovery confirms a build and mints a capability: design routes only.
                    legacy_short_circuit = await resolve_legacy_preview_short_circuit(session, conversation_id, user_message, identity.name, identity.email, shop_domain)

                if deterministic_reply is not None:
                    # ATTACK / OFF_TOPIC / SERVICE_META / INVALID / UNRESOLVED: no model, no
                    # extraction, no tools, no external calls, no legacy recovery, no profile
                    # writes, no generation. Server-authored reply only.
                    reply_text = deterministic_reply
                    sse_events = []
                    updated_messages = [*history, {"role": "assistant", "content": reply_text}]
                elif legacy_short_circuit:
                    reply_text = "Pulling up your fragrance preview now."
                    sse_events = [{
                        "type": "preview_ready", "recommendationId": legacy_short_circuit["recommendationId"],
                        "previewId": legacy_short_circuit["recommendationId"], "previewUrl": legacy_short_circuit["previewUrl"],
                    }]
                    updated_messages = [*history, {"role": "assistant", "content": reply_text}]
                else:
                    result = await asyncio.wait_for(
                        call_ai(session, history, conversation_id, identity.email, identity.name, shop_domain, gate=gate),
                        timeout=settings.chat_turn_deadline_seconds,
                    )
                    reply_text, sse_events, updated_messages = result["replyText"], result["sseEvents"], result.get("updatedMessages")

                set_conversation_cache(conversation_id, updated_messages or history)

                try:
                    await create_or_update_conversation(session, conversation_id, identity.email, identity.name)
                    try:
                        # Raw message + classification in ONE transaction (Phase 4A).
                        await save_user_message_with_classification(session, conversation_id, user_message, classification=gate.classification, reason_code=gate.reason_code, version=gate.version)
                    except Exception as err:
                        # e.g. migration 0003 not applied. The raw turn is still stored for the
                        # customer's history; with no classification it is replayed to a model on
                        # reload only if layer 1 confidently accepts it (project_stored_turn), so
                        # a semantically detected attack is never restored as safe.
                        logger.error("SECURITY_CLASSIFICATION_PERSIST_FAILED %s", type(err).__name__)
                        await save_message(session, conversation_id, "user", user_message)
                    await save_message(session, conversation_id, "assistant", reply_text)
                except Exception as err:
                    logger.error("Failed to persist chat log: %s", type(err).__name__)

                # preview_ready makes the widget navigate away the instant it's parsed, so it must
                # never reach the client before the reasoning bridge text. Every other event keeps
                # its position ahead of the chunk.
                preview_ready_event = None
                id_event: dict[str, Any] = {"type": "id", "conversation_id": conversation_id}
                if new_token:
                    id_event["conversation_token"] = new_token
                    id_event["expires_at"] = new_token_expires_at
                yield _sse_line(id_event)
                for event in sse_events or []:
                    if event.get("type") == "preview_ready":
                        preview_ready_event = event
                        continue
                    safe = public_sse_event(event)
                    if safe:
                        yield _sse_line(safe)
                yield _sse_line({"type": "chunk", "chunk": reply_text})
                yield _sse_line({"type": "message_complete"})
                if preview_ready_event:
                    logger.info("CHAT_PREVIEW_EVENT %s", json.dumps({
                        "conversationId": conversation_id, "recommendationId": preview_ready_event.get("recommendationId"),
                        "eventType": preview_ready_event["type"], "previewUrl": preview_url_for_logging(preview_ready_event.get("previewUrl")),
                    }))
                    yield _sse_line(public_sse_event(preview_ready_event))
                yield _sse_line({"type": "end_turn"})
            except asyncio.TimeoutError:
                logger.error("CHAT_TURN_DEADLINE_EXCEEDED %s", json.dumps({"conversationId": conversation_id}))
                yield _sse_line({"type": "error", "error": "That took too long. Please try again."})
            except Exception as err:
                logger.error("Action error: %s", type(err).__name__, exc_info=True)
                yield _sse_line({"type": "error", "error": "Error processing request."})
        finally:
            turn_slots.release()
            await lock.__aexit__(None, None, None)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "Connection": "keep-alive"},
    )


@router.post("/internal/chat", dependencies=[Depends(require_internal_api_key)])
async def internal_chat_action(request: Request, body: ChatRequest, session: AsyncSession = Depends(get_session)) -> StreamingResponse:
    return await _run_chat_turn(request, body, session, public=False)


@router.post("/chat")
async def chat_action(request: Request, body: ChatRequest, session: AsyncSession = Depends(get_session)) -> StreamingResponse:
    return await _run_chat_turn(request, body, session, public=True)
