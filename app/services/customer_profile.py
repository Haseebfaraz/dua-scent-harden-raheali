"""Port of app/services/customerProfile.server.js -- CustomerFragranceProfile state, stored
separately from raw conversation history and validated by the backend rather than left to the
model's own memory of "which turn am I on."

weatherDirection is fully automatic (derived and saved the moment a city is verified). Season is
NEVER persisted here at all -- see app/fragrance/weather.py's own module docstring.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.ids import new_id
from app.db.models import CustomerProfileState
from app.db.time import utcnow

VALID_SEASONS = ["Winter", "Spring", "Summer", "Fall"]
VALID_STRENGTH_PREFERENCES = ["light", "moderate", "strong"]
VALID_WEATHER_DIRECTIONS = ["hot", "warm", "mild", "cool", "cold", "humid", "rainy"]
VALID_LOCATION_SOURCES = ["geocoding", "order_history", "customer_confirmed"]


def empty_profile() -> dict[str, Any]:
    return {
        "name": None,
        "email": None,
        "city": None,
        "stateRegion": None,
        "country": None,
        "requestedSeasonStyle": None,
        "seasonStyleConflictResolved": False,
        "currentWeather": {"condition": None, "temperatureC": None, "fetchedAt": None},
        "weatherDirection": None,
        "weatherLocation": {"city": None, "country": None, "verified": False},
        "likes": [],
        "dislikes": [],
        "preferenceVocabularyCorrections": [],
        "preferredStyle": None,
        "inferredStyle": None,
        "occasion": None,
        "giftRecipient": None,
        "dislikesAsked": False,
        "occasionAsked": False,
        "strengthPreference": None,
        "additionalPreferences": [],
        "locationVerified": False,
        "locationSource": None,
        "selectedRecommendationId": None,
        "pendingRecreateRecommendationId": None,
    }


async def get_customer_profile(session: AsyncSession, conversation_id: str) -> dict[str, Any]:
    row = await session.scalar(
        select(CustomerProfileState).where(CustomerProfileState.conversationId == conversation_id)
    )
    empty = empty_profile()
    if not row:
        return empty
    stored = row.profileJson
    return {
        **empty,
        **stored,
        "currentWeather": {**empty["currentWeather"], **(stored.get("currentWeather") or {})},
        "weatherLocation": {**empty["weatherLocation"], **(stored.get("weatherLocation") or {})},
    }


async def _upsert_profile(session: AsyncSession, conversation_id: str, updated: dict[str, Any]) -> None:
    row = await session.scalar(
        select(CustomerProfileState).where(CustomerProfileState.conversationId == conversation_id)
    )
    now = utcnow()
    if row:
        row.profileJson = updated
        row.updatedAt = now
    else:
        session.add(
            CustomerProfileState(
                id=new_id(),
                conversationId=conversation_id,
                profileJson=updated,
                createdAt=now,
                updatedAt=now,
            )
        )
    await session.commit()


async def save_customer_profile_field(
    session: AsyncSession, conversation_id: str, field: str, value: Any
) -> dict[str, Any]:
    current = await get_customer_profile(session, conversation_id)
    if field not in current:
        raise ValueError(f'Unknown CustomerFragranceProfile field: "{field}"')
    updated = {**current, field: value}
    await _upsert_profile(session, conversation_id, updated)
    return updated


async def save_customer_profile_fields(
    session: AsyncSession, conversation_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    current = await get_customer_profile(session, conversation_id)
    for field in fields:
        if field not in current:
            raise ValueError(f'Unknown CustomerFragranceProfile field: "{field}"')
    updated = {**current, **fields}
    await _upsert_profile(session, conversation_id, updated)
    return updated


def get_missing_required_fields(profile: dict[str, Any]) -> list[str]:
    missing = []
    if not profile.get("city") or not profile.get("locationVerified"):
        missing.append("city")
    if not profile.get("country"):
        missing.append("country")
    if not (profile.get("likes") and len(profile["likes"]) > 0) and not profile.get("preferredStyle") and not profile.get("inferredStyle"):
        missing.append("likes or preferredStyle")
    if not profile.get("dislikesAsked"):
        missing.append("dislikesAsked (ask about dislikes, even if the real answer is none)")
    if not profile.get("occasionAsked"):
        missing.append("occasionAsked (ask about occasion, even if the real answer is just everyday)")
    return missing


def is_profile_ready_for_analysis(profile: dict[str, Any]) -> bool:
    return len(get_missing_required_fields(profile)) == 0
