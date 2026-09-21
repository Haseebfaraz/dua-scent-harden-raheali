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
from app.services.data_lifecycle import ensure_conversation_writable

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
        "locationAsked": False,
        "nameAsked": False,
        "strengthPreference": None,
        "additionalPreferences": [],
        "locationVerified": False,
        "locationSource": None,
        "selectedRecommendationId": None,
        "pendingRecreateRecommendationId": None,
        "fragrancePivotOffered": False,
        "fragrancePivotDeclined": False,
        "customBuildInvited": False,
        "customBuildAccepted": False,
        "customBuildDeclined": False,
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
    await ensure_conversation_writable(session, conversation_id)  # Phase 6: a deleted conversation is never written back
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
    """Discovery-completeness policy (Phase 8) -- replaces the old "style direction + any ONE
    other signal" binary, which let a recommendation build after just two weak facts (e.g. "strong
    oud" + a passing "warm days" mention was already enough). Verified live that this was firing a
    recommendation several turns before the customer had actually given enough to go on.

    Every dimension below must be independently covered, though a single customer message can
    supply several of them at once -- this is not a fixed question order or a rigid checklist read
    back to the customer. The *Asked flags exist so a genuinely-answered "none"/"no preference" or
    a customer who can't/won't give a city still counts as resolved -- this never re-asks and never
    loops forever chasing a fact the customer isn't going to give.

    Conceptually this is a 3-state model (DISCOVERY_INCOMPLETE / DISCOVERY_SUFFICIENT /
    READY_TO_BUILD) collapsed into a single "what's still missing" list, since there is exactly one
    consumer decision it drives: can analyze_customer_product_candidates run yet. An empty list is
    READY_TO_BUILD; anything else is DISCOVERY_INCOMPLETE.
    """
    missing: list[str] = []

    has_style_direction = bool(profile.get("likes")) or bool(profile.get("preferredStyle")) or bool(profile.get("inferredStyle")) or bool(profile.get("additionalPreferences"))
    if not has_style_direction:
        missing.append("a fragrance direction, style, or vibe (likes, preferredStyle, or a mood word like seductive/clean/bold)")

    if not (bool(profile.get("dislikes")) or bool(profile.get("dislikesAsked"))):
        missing.append("dislikes or hard exclusions (or an explicit 'nothing I dislike')")

    # A known gift recipient already establishes real use-context (who it's for) -- requiring a
    # separate, distinct "occasion" fact on top of that would just be a second, redundant question.
    if not (bool(profile.get("occasion")) or bool(profile.get("occasionAsked")) or bool(profile.get("giftRecipient"))):
        missing.append("occasion or use context")

    if not bool(profile.get("strengthPreference")):
        missing.append("performance preference (longevity, projection, or strength)")

    if not ((bool(profile.get("city")) and bool(profile.get("locationVerified"))) or bool(profile.get("locationAsked"))):
        missing.append("location, for verified weather/season context")

    # A Shopify account is not guaranteed to have a name on file (email-only accounts are common),
    # and nothing else in the conversation forces this to be asked -- without this dimension, a
    # nameless account could sail through discovery and only discover it's blocked at the very
    # final confirm_recommendation identity check, with no natural point earlier to have fixed it.
    if not (bool(profile.get("name")) or bool(profile.get("nameAsked"))):
        missing.append("the customer's name")

    return missing


def is_profile_ready_for_analysis(profile: dict[str, Any]) -> bool:
    return len(get_missing_required_fields(profile)) == 0
