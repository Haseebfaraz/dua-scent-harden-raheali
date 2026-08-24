"""FastAPI chat endpoints -- both the internal Node-proxy path (Phase 8) and the direct public
path the storefront widget calls once Node is no longer in the loop (Phase 6). Same underlying
logic either way; the only difference is auth (a shared internal key for the server-to-server
hop vs. nothing, since a browser can't safely hold a shared secret) and where shop_domain comes
from (Node already resolved it; a direct browser call never sends one, so Python resolves it
itself the same way shopDomain.server.js always did).
"""

import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.conversation_flow import call_ai, get_conversation, set_conversation_cache
from app.config import settings
from app.db.session import get_session
from app.schemas.chat import ChatRequest
from app.services.conversation import create_or_update_conversation, save_message
from app.services.customer_profile import get_customer_profile, save_customer_profile_field
from app.services.legacy_preview_recovery import resolve_legacy_preview_short_circuit
from app.shopify.sessions import resolve_shop_domain

logger = logging.getLogger(__name__)
router = APIRouter()


def require_internal_api_key(x_internal_api_key: str | None = Header(default=None)) -> None:
    """This is a private service-to-service hop (Node's Shopify adapter -> this FastAPI service),
    never called directly by a browser -- a shared key header, not customer auth. Only enforced
    when INTERNAL_API_KEY is actually configured, so local dev without it set still works.
    """
    if settings.internal_api_key and x_internal_api_key != settings.internal_api_key:
        raise HTTPException(status_code=401, detail="invalid or missing internal API key")


def _sse_line(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


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
                logger.error("Failed to persist recreate re-entry message: %s", err)
            await save_customer_profile_field(session, conversation_id, "pendingRecreateRecommendationId", None)

    return {
        "messages": [
            {"role": m["role"], "content": m["content"]}
            for m in history
            if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m["content"].strip()
        ]
    }


@router.get("/internal/chat/history", dependencies=[Depends(require_internal_api_key)])
async def chat_history(conversation_id: str | None = None, session: AsyncSession = Depends(get_session)) -> dict:
    return await _history_payload(session, conversation_id)


@router.get("/chat")
async def public_chat_history(history: str | None = None, conversation_id: str | None = None, session: AsyncSession = Depends(get_session)) -> dict:
    # Matches chat.jsx's loader: only a genuine ?history=true request returns real history, same
    # as the widget's own fetchChatHistory() call shape -- anything else gets an empty list.
    if history != "true":
        return {"messages": []}
    return await _history_payload(session, conversation_id)


@router.post("/internal/chat", dependencies=[Depends(require_internal_api_key)])
@router.post("/chat")
async def chat_action(body: ChatRequest, session: AsyncSession = Depends(get_session)) -> StreamingResponse:
    shop_domain = body.shop_domain or await resolve_shop_domain(session)

    async def _stream():
        try:
            user_message = body.message or ""
            conv = await get_conversation(session, body.conversation_id)
            conversation_id, history = conv["id"], conv["history"]

            greeting_text = body.greeting.strip() if not history and isinstance(body.greeting, str) else ""
            if greeting_text:
                history.append({"role": "assistant", "content": greeting_text})
            history.append({"role": "user", "content": user_message})

            known_customer_email = body.customer_email.strip() if body.customer_email and "@" in body.customer_email else None
            known_customer_name = body.customer_name.strip() if body.customer_name and body.customer_name.strip() else None

            legacy_short_circuit = await resolve_legacy_preview_short_circuit(session, conversation_id, user_message, known_customer_name, known_customer_email, shop_domain)

            if legacy_short_circuit:
                reply_text = "Pulling up your fragrance preview now."
                sse_events = [{
                    "type": "preview_ready", "recommendationId": legacy_short_circuit["recommendationId"],
                    "previewId": legacy_short_circuit["recommendationId"], "previewUrl": legacy_short_circuit["previewUrl"],
                }]
                updated_messages = [*history, {"role": "assistant", "content": reply_text}]
            else:
                result = await call_ai(session, history, conversation_id, known_customer_email, known_customer_name, shop_domain)
                reply_text, sse_events, updated_messages = result["replyText"], result["sseEvents"], result.get("updatedMessages")

            set_conversation_cache(conversation_id, updated_messages or history)

            try:
                await create_or_update_conversation(session, conversation_id, known_customer_email, known_customer_name)
                if greeting_text:
                    await save_message(session, conversation_id, "assistant", greeting_text)
                await save_message(session, conversation_id, "user", user_message)
                await save_message(session, conversation_id, "assistant", reply_text)
            except Exception as err:
                logger.error("Failed to persist chat log: %s", err)

            # preview_ready makes the widget navigate away the instant it's parsed
            # (handlePreviewReady runs before chat.js's own event-type switch) -- so it must
            # never reach the client before the reasoning bridge that explains the pick. Every
            # other sse_event keeps its original position ahead of the chunk; preview_ready alone
            # is held back until after the text is fully delivered.
            preview_ready_event = None
            yield _sse_line({"type": "id", "conversation_id": conversation_id})
            for event in sse_events or []:
                if event.get("type") == "preview_ready":
                    preview_ready_event = event
                    continue
                yield _sse_line(event)
            yield _sse_line({"type": "chunk", "chunk": reply_text})
            yield _sse_line({"type": "message_complete"})
            if preview_ready_event:
                logger.info("CHAT_PREVIEW_EVENT %s", json.dumps({
                    "conversationId": conversation_id, "recommendationId": preview_ready_event.get("recommendationId"),
                    "eventType": preview_ready_event["type"], "previewUrl": preview_ready_event.get("previewUrl"),
                }))
                yield _sse_line(preview_ready_event)
            yield _sse_line({"type": "end_turn"})
        except Exception as err:
            logger.error("Action error: %s", err, exc_info=True)
            yield _sse_line({"type": "error", "error": "Error processing request."})

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
