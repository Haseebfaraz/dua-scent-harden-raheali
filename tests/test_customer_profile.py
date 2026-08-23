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
    profile = empty_profile()
    assert get_missing_required_fields(profile) == [
        "city", "country", "likes or preferredStyle",
        "dislikesAsked (ask about dislikes, even if the real answer is none)",
        "occasionAsked (ask about occasion, even if the real answer is just everyday)",
    ]
    assert is_profile_ready_for_analysis(profile) is False


def test_ready_when_all_required_fields_present_no_season():
    profile = {
        **empty_profile(), "city": "Los Angeles", "country": "United States", "likes": ["Fruity"],
        "locationVerified": True, "dislikesAsked": True, "occasionAsked": True,
    }
    assert get_missing_required_fields(profile) == []
    assert is_profile_ready_for_analysis(profile) is True


def test_accepts_preferred_style_in_place_of_likes():
    profile = {
        **empty_profile(), "city": "Los Angeles", "country": "United States", "preferredStyle": "warm and woody",
        "locationVerified": True, "dislikesAsked": True, "occasionAsked": True,
    }
    assert is_profile_ready_for_analysis(profile) is True


def test_empty_dislikes_never_blocks_readiness():
    profile = {
        **empty_profile(), "city": "A", "country": "B", "likes": ["Fruity"], "dislikes": [],
        "locationVerified": True, "dislikesAsked": True, "occasionAsked": True,
    }
    assert is_profile_ready_for_analysis(profile) is True


def test_blocks_until_dislikes_and_occasion_asked():
    profile = {
        **empty_profile(), "city": "Los Angeles", "country": "United States", "likes": ["Fruity"], "locationVerified": True,
    }
    assert get_missing_required_fields(profile) == [
        "dislikesAsked (ask about dislikes, even if the real answer is none)",
        "occasionAsked (ask about occasion, even if the real answer is just everyday)",
    ]
    assert is_profile_ready_for_analysis(profile) is False


def test_unverified_city_blocks_readiness():
    profile = {
        **empty_profile(), "city": "Vice City", "country": "United States", "likes": ["Fruity"], "locationVerified": False,
        "dislikesAsked": True, "occasionAsked": True,
    }
    assert get_missing_required_fields(profile) == ["city"]
    assert is_profile_ready_for_analysis(profile) is False


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
