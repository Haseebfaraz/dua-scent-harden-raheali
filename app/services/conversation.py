"""Port of the conversation-persistence helpers in app/db.server.js -- durable storage for
Conversation/Message, independent of whatever in-memory cache the chat layer keeps on top.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.ids import new_id
from app.db.models import Conversation, Message
from app.db.time import utcnow


async def create_or_update_conversation(
    session: AsyncSession, conversation_id: str, customer_email: str | None = None, customer_name: str | None = None
) -> Conversation:
    existing = await session.scalar(select(Conversation).where(Conversation.id == conversation_id))
    now = utcnow()
    if existing:
        existing.updatedAt = now
        if customer_email:
            existing.customerEmail = customer_email
        if customer_name:
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
