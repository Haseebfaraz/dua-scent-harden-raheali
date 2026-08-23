"""Read-only internal endpoint exposing a confirmed/pending recommendation's customer-safe shape.
Not on the current live path (Node's preview page still reads the same Postgres row directly via
Prisma) -- provided so a future Node change can fetch this over HTTP instead, without duplicating
recommendation_confirmation.py's allow-list logic on the Node side.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.chat import require_internal_api_key
from app.db.session import get_session
from app.services.recommendation_confirmation import get_recommendation, to_customer_safe_recommendation

router = APIRouter()


@router.get("/internal/recommendations/{recommendation_id}", dependencies=[Depends(require_internal_api_key)])
async def read_recommendation(recommendation_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    record = await get_recommendation(session, recommendation_id)
    if not record:
        raise HTTPException(status_code=404, detail="recommendation not found")
    return to_customer_safe_recommendation(record)
