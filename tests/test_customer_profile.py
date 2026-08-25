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
    # Phase 8: discovery completeness -- with zero style direction at all, that's the one and only
    # thing worth surfacing first; the other dimensions all report missing too but style leads.
    profile = empty_profile()
    missing = get_missing_required_fields(profile)
    assert missing[0] == "a fragrance direction, style, or vibe (likes, preferredStyle, or a mood word like seductive/clean/bold)"
    assert is_profile_ready_for_analysis(profile) is False


def test_style_direction_alone_is_not_enough_without_any_other_signal():
    profile = {**empty_profile(), "likes": ["Fruity"]}
    assert len(get_missing_required_fields(profile)) == 4  # dislikes, occasion, performance, location all still missing
    assert is_profile_ready_for_analysis(profile) is False


def test_strong_oud_plus_warm_weather_alone_is_not_ready():
    # The exact regression this guards: a live conversation ("...i like strong (oud type)" / "for
    # the warm days.") generated a recommendation off just style + a passing weather mention. That
    # must NOT be enough on its own even with strengthPreference AND a verified, warm location --
    # dislikes and occasion are both still ungathered.
    profile = {
        **empty_profile(), "likes": ["Oud"], "strengthPreference": "strong",
        "city": "Los Angeles", "country": "United States", "locationVerified": True, "weatherDirection": "warm",
    }
    missing = get_missing_required_fields(profile)
    assert "dislikes or hard exclusions (or an explicit 'nothing I dislike')" in missing
    assert "occasion or use context" in missing
    assert is_profile_ready_for_analysis(profile) is False


def test_all_discovery_dimensions_present_is_ready():
    # style/vibe + occasion + dislike + performance + verified location/weather, matching the
    # explicit "clearly enough" example: strong oud, date night, dislike vanilla, strong
    # projection, Los Angeles, warm weather verified, dark/seductive vibe.
    profile = {
        **empty_profile(),
        "likes": ["Oud", "Seductive"], "dislikes": ["Vanilla"], "occasion": "date night",
        "strengthPreference": "strong",
        "city": "Los Angeles", "country": "United States", "locationVerified": True, "weatherDirection": "warm",
    }
    assert get_missing_required_fields(profile) == []
    assert is_profile_ready_for_analysis(profile) is True


def test_asked_flags_satisfy_their_dimension_without_a_real_answer():
    # A genuinely-confirmed "none"/"couldn't give a city" must count as resolved -- these must
    # never be re-asked, and must never block readiness forever chasing a fact that isn't coming.
    profile = {
        **empty_profile(), "likes": ["Fruity"], "dislikesAsked": True, "occasionAsked": True,
        "strengthPreference": "moderate", "locationAsked": True,
    }
    assert get_missing_required_fields(profile) == []
    assert is_profile_ready_for_analysis(profile) is True


def test_gift_recipient_satisfies_occasion_without_a_separate_occasion_fact():
    profile = {
        **empty_profile(), "likes": ["Fruity"], "giftRecipient": "wife", "dislikesAsked": True,
        "strengthPreference": "light", "locationAsked": True,
    }
    assert get_missing_required_fields(profile) == []
    assert is_profile_ready_for_analysis(profile) is True


def test_unverified_location_does_not_satisfy_the_location_dimension():
    # An unverified/implausible city is not the same as a verified one, and is not the same as
    # having genuinely asked and moved on -- it must still count as missing.
    profile = {
        **empty_profile(), "likes": ["Fruity"], "dislikesAsked": True, "occasionAsked": True,
        "strengthPreference": "moderate", "city": "Vice City", "locationVerified": False,
    }
    assert "location, for verified weather/season context" in get_missing_required_fields(profile)
    assert is_profile_ready_for_analysis(profile) is False


def test_weather_direction_is_never_a_model_settable_field():
    # Structural guarantee that the model can never fabricate weather: weatherDirection/
    # currentWeather are not in the tool's settable field list at all -- they can only ever be
    # written by verify_customer_location's own backend code path.
    from app.ai.tools import PROFILE_FIELD_NAMES

    assert "weatherDirection" not in PROFILE_FIELD_NAMES
    assert "currentWeather" not in PROFILE_FIELD_NAMES


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
