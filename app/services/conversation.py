"""Port of the conversation-persistence helpers in app/db.server.js -- durable storage for
Conversation/Message, independent of whatever in-memory cache the chat layer keeps on top.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.ids import new_id
from app.db.models import Conversation, Message, MessageSecurityClassification
from app.db.time import utcnow


async def create_or_update_conversation(
    session: AsyncSession, conversation_id: str, customer_email: str | None = None, customer_name: str | None = None
) -> Conversation:
    existing = await session.scalar(select(Conversation).where(Conversation.id == conversation_id))
    now = utcnow()
    if existing:
        existing.updatedAt = now
        # Phase 2 (F8): self-reported contact data never overwrites what is already recorded.
        if customer_email and not existing.customerEmail:
            existing.customerEmail = customer_email
        if customer_name and not existing.customerName:
            existing.customerName = customer_name
        await session.commit()
        return existing

    conversation = Conversation(
        id=conversation_id, customerEmail=customer_email or None, customerName=customer_name or None,
        createdAt=now, updatedAt=now,
    )
    session.add(conversation)
    await session.commit()
    return conversation


async def save_message(session: AsyncSession, conversation_id: str, role: str, content: str) -> Message:
    await create_or_update_conversation(session, conversation_id)
    message = Message(id=new_id(), conversationId=conversation_id, role=role, content=content, createdAt=utcnow())
    session.add(message)
    await session.commit()
    return message


async def get_conversation_history(session: AsyncSession, conversation_id: str) -> list[Message]:
    return list(
        (
            await session.execute(
                select(Message).where(Message.conversationId == conversation_id).order_by(Message.createdAt.asc())
            )
        ).scalars()
    )


async def save_user_message_with_classification(session: AsyncSession, conversation_id: str, content: str, *, classification: str, reason_code: str, version: str) -> Message:
    """Phase 4A: the raw customer message and its classification are written in ONE transaction,
    so a stored customer turn can never exist without its classification because the second write
    failed. On any error nothing is written and the exception propagates to the caller."""
    await create_or_update_conversation(session, conversation_id)
    message = Message(id=new_id(), conversationId=conversation_id, role="user", content=content, createdAt=utcnow())
    session.add(message)
    session.add(MessageSecurityClassification(id=new_id(), messageId=message.id, classification=classification, reasonCode=reason_code, classifierVersion=version, createdAt=utcnow()))
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return message


async def save_message_classification(session: AsyncSession, message_id: str, *, classification: str, reason_code: str, version: str) -> None:
    """Phase 4: persist the scope/security classification of a customer message (operational
    codes only). Failures are logged by the caller; the chat never depends on this write."""
    session.add(MessageSecurityClassification(id=new_id(), messageId=message_id, classification=classification, reasonCode=reason_code, classifierVersion=version, createdAt=utcnow()))
    await session.commit()


async def get_message_classifications(session: AsyncSession, message_ids: list[str]) -> dict[str, str]:
    """messageId -> classification for the given messages (missing = unclassified / legacy)."""
    if not message_ids:
        return {}
    rows = (await session.execute(select(MessageSecurityClassification.messageId, MessageSecurityClassification.classification).where(MessageSecurityClassification.messageId.in_(message_ids)))).all()
    return {message_id: classification for message_id, classification in rows}
