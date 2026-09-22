"""Conversation ownership (Phase 2, finding F7).

A conversation id identifies a record. It is not authorization. Ownership of a guest conversation
is proven by possession of a conversation capability: a random 256-bit secret the server mints
when it creates the conversation, returns to that browser exactly once, and stores only as a
SHA-256 hash. Every public read or write of a conversation must present it.

Properties:
  * server generated (`secrets.token_urlsafe(32)`), never derived from the conversation id,
    email, name, IP, or anything a caller can choose;
  * scoped to exactly one conversation;
  * expires (CONVERSATION_TOKEN_TTL_DAYS) and can be revoked;
  * only the hash is stored; verification is a hash lookup plus constant-time comparisons;
  * never logged, never placed in message content or model context, never put in a URL --
    it travels in the `X-Conversation-Token` header (or the JSON body for the chat POST).

This capability is deliberately separate from the Phase 1 build capability: owning a conversation
never authorizes a Shopify build mutation, and vice versa.

A Shopify-signed `logged_in_customer_id` seen through the App Proxy can be BOUND to a capability
(verifiedShopifyCustomerId); after that, requests carrying a different signed customer are refused
even with the token. Self-reported name/email never participate in any of this.
"""

import hashlib
import hmac
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.ids import new_id
from app.db.models import Conversation, ConversationCapability
from app.db.time import utcnow

CONVERSATION_TOKEN_HEADER = "X-Conversation-Token"
_MAX_TOKEN_LENGTH = 128
# lastUsedAt is refreshed at most this often, so a chat turn costs one write, not one per chunk.
_LAST_USED_REFRESH = timedelta(minutes=10)


class ConversationNotAuthorized(Exception):
    """Missing, malformed, unknown, expired, revoked, or mismatched capability. One message for
    every case so the response never reveals whether the conversation exists."""

    def __init__(self, message: str = "This conversation session is not valid or has expired. Please start a new conversation."):
        super().__init__(message)


def hash_conversation_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_conversation_with_capability(
    session: AsyncSession, *, verified_shopify_customer_id: str | None = None
) -> tuple[str, str, ConversationCapability]:
    """Server-controlled bootstrap: mint the conversation id AND its secret together. The caller
    never chooses either. Returns (conversation_id, plaintext_token, capability)."""
    now = utcnow()
    conversation_id = new_id()
    session.add(Conversation(id=conversation_id, customerEmail=None, customerName=None, createdAt=now, updatedAt=now))
    token = secrets.token_urlsafe(32)
    capability = ConversationCapability(
        id=new_id(),
        conversationId=conversation_id,
        tokenHash=hash_conversation_token(token),
        verifiedShopifyCustomerId=verified_shopify_customer_id or None,
        expiresAt=now + timedelta(days=settings.conversation_token_ttl_days),
        revokedAt=None,
        lastUsedAt=now,
        createdAt=now,
    )
    session.add(capability)
    await session.commit()
    return conversation_id, token, capability


async def authorize_conversation(
    session: AsyncSession, *, token: object, conversation_id: object, verified_shopify_customer_id: str | None = None,
    touch: bool = False,
) -> ConversationCapability:
    """Return the live capability proving ownership of `conversation_id`, or raise
    ConversationNotAuthorized. If the capability is bound to a Shopify customer and the request
    carries a different signed customer id, refuse."""
    if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN_LENGTH:
        raise ConversationNotAuthorized()
    if not isinstance(conversation_id, str) or not conversation_id:
        raise ConversationNotAuthorized()
    row = await session.scalar(select(ConversationCapability).where(ConversationCapability.tokenHash == hash_conversation_token(token)))
    if row is None:
        raise ConversationNotAuthorized()
    if not hmac.compare_digest(row.conversationId.encode(), conversation_id.encode()):
        raise ConversationNotAuthorized()
    if row.revokedAt is not None:
        raise ConversationNotAuthorized()
    now = utcnow()
    if row.expiresAt <= now:
        raise ConversationNotAuthorized()
    if row.verifiedShopifyCustomerId and verified_shopify_customer_id and not hmac.compare_digest(row.verifiedShopifyCustomerId.encode(), verified_shopify_customer_id.encode()):
        raise ConversationNotAuthorized()
    if touch and (row.lastUsedAt is None or now - row.lastUsedAt > _LAST_USED_REFRESH):
        row.lastUsedAt = now
        await session.commit()
    return row


async def bind_verified_customer(session: AsyncSession, capability: ConversationCapability, verified_shopify_customer_id: str) -> None:
    """Explicit upgrade: a guest capability meets a Shopify-signed customer id for the first
    time. Never silently replaces an existing binding (authorize_conversation already refuses
    a mismatch before this can run)."""
    if capability.verifiedShopifyCustomerId:
        return
    capability.verifiedShopifyCustomerId = verified_shopify_customer_id
    await session.commit()


async def revoke_conversation_tokens(session: AsyncSession, conversation_id: str) -> int:
    rows = (await session.execute(select(ConversationCapability).where(ConversationCapability.conversationId == conversation_id, ConversationCapability.revokedAt.is_(None)))).scalars().all()
    now = utcnow()
    for row in rows:
        row.revokedAt = now
    await session.commit()
    return len(rows)

