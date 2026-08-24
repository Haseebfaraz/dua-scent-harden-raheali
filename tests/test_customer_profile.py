import uuid

import pytest
from sqlalchemy import delete

from app.db.models import CustomerProfileState
from app.services.customer_profile import (
    empty_profile,
    get_customer_profile,
    get_missing_required_fields,
    is_profile_ready_for_analysis,
    save_customer_profile_field,
)


def _new_conversation_id() -> str:
    return f"pytest-profile-{uuid.uuid4().hex}"


def test_missing_required_fields_on_empty_profile():
    # Phase 7: confidence-based readiness, not a fixed checklist -- with zero style direction at
    # all, that's the one and only thing worth asking about; nothing else matters yet.
    profile = empty_profile()
    assert get_missing_required_fields(profile) == ["likes or preferredStyle"]
    assert is_profile_ready_for_analysis(profile) is False


def test_style_direction_alone_is_not_enough_without_any_other_signal():
    profile = {**empty_profile(), "likes": ["Fruity"]}
    assert get_missing_required_fields(profile) == [
        "at least one more high-value signal (occasion, dislikes, gift context, strength preference, or a verified location)"
    ]
    assert is_profile_ready_for_analysis(profile) is False


def test_style_plus_occasion_is_enough_no_location_or_dislikes_needed():
    # The wedding/work-party example: a style direction plus a real occasion is enough to
    # generate -- city, country, and an explicit "did you ask about dislikes" flag are no longer
    # hard requirements.
    profile = {**empty_profile(), "preferredStyle": "fresh", "occasion": "wedding"}
    assert get_missing_required_fields(profile) == []
    assert is_profile_ready_for_analysis(profile) is True


def test_accepts_preferred_style_in_place_of_likes():
    profile = {**empty_profile(), "preferredStyle": "warm and woody", "dislikesAsked": True}
    assert is_profile_ready_for_analysis(profile) is True


def test_empty_dislikes_asked_still_counts_as_a_real_signal():
    # A deliberately-confirmed "no dislikes" is still worth something, even with an empty list.
    profile = {**empty_profile(), "likes": ["Fruity"], "dislikes": [], "dislikesAsked": True}
    assert is_profile_ready_for_analysis(profile) is True


def test_verified_location_alone_can_satisfy_readiness_without_dislikes_or_occasion():
    profile = {
        **empty_profile(), "likes": ["Fruity"], "city": "Los Angeles", "country": "United States", "locationVerified": True,
    }
    assert get_missing_required_fields(profile) == []
    assert is_profile_ready_for_analysis(profile) is True


def test_unverified_or_unknown_location_never_blocks_readiness_on_its_own():
    # Phase 7: don't ask for location unless it will affect the recommendation -- an unverified
    # city must never be treated as a blocker by itself once another real signal exists.
    profile = {
        **empty_profile(), "city": "Vice City", "country": "United States", "likes": ["Fruity"],
        "locationVerified": False, "dislikesAsked": True,
    }
    assert get_missing_required_fields(profile) == []
    assert is_profile_ready_for_analysis(profile) is True


@pytest.mark.asyncio
async def test_returns_empty_profile_for_unseen_conversation(db_session):
    profile = await get_customer_profile(db_session, _new_conversation_id())
    assert profile == empty_profile()


@pytest.mark.asyncio
async def test_persists_and_rehydrates_a_field(db_session):
    conversation_id = _new_conversation_id()
    try:
        await save_customer_profile_field(db_session, conversation_id, "city", "Los Angeles")
        await save_customer_profile_field(db_session, conversation_id, "likes", ["Fruity", "Sweet"])

        reread = await get_customer_profile(db_session, conversation_id)
        assert reread["city"] == "Los Angeles"
        assert reread["likes"] == ["Fruity", "Sweet"]
    finally:
        await db_session.execute(delete(CustomerProfileState).where(CustomerProfileState.conversationId == conversation_id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_rejects_unknown_profile_field(db_session):
    with pytest.raises(ValueError, match="Unknown CustomerFragranceProfile field"):
        await save_customer_profile_field(db_session, _new_conversation_id(), "notARealField", "x")
