from app.fragrance.weather import (
    describe_weather_simple,
    derive_weather_direction,
    get_calendar_season,
    has_season_weather_conflict,
    weather_direction_to_query_season,
)


def test_get_calendar_season_returns_one_of_four_seasons():
    for country in [None, "United States", "Australia", "Chile"]:
        assert get_calendar_season(country) in ("Winter", "Spring", "Summer", "Fall")


def test_get_calendar_season_flips_north_south():
    northern = get_calendar_season("United States")
    southern = get_calendar_season("Australia")
    flip = {"Winter": "Summer", "Summer": "Winter", "Spring": "Fall", "Fall": "Spring"}
    assert southern == flip[northern]


def test_describe_weather_simple_never_states_exact_temperature():
    result = describe_weather_simple(95, 0)
    assert "hot" in result["words"]
    assert "sunny" in result["words"]
    assert not any(ch.isdigit() for ch in result["summary"])


def test_describe_weather_simple_rainy_and_cool():
    result = describe_weather_simple(50, 63)
    assert "cool" in result["words"]
    assert "rainy" in result["words"]


def test_derive_weather_direction_rainy_regardless_of_temperature():
    assert derive_weather_direction(80, 63) == "rainy"


def test_derive_weather_direction_humid():
    assert derive_weather_direction(80, 0, 70) == "humid"


def test_derive_weather_direction_cold_high_humidity_is_not_humid():
    assert derive_weather_direction(45, 0, 90) != "humid"


def test_derive_weather_direction_temperature_bands():
    assert derive_weather_direction(95, 0) == "hot"
    assert derive_weather_direction(75, 0) == "warm"
    assert derive_weather_direction(60, 0) == "mild"
    assert derive_weather_direction(45, 0) == "cool"
    assert derive_weather_direction(20, 0) == "cold"


def test_weather_direction_to_query_season():
    assert weather_direction_to_query_season("hot", "Spring") == "Summer"
    assert weather_direction_to_query_season("humid", "Spring") == "Summer"
    assert weather_direction_to_query_season("cold", "Spring") == "Winter"


def test_weather_direction_to_query_season_defers_to_fallback():
    assert weather_direction_to_query_season("mild", "Fall") == "Fall"
    assert weather_direction_to_query_season(None, "Fall") == "Fall"


def test_has_season_weather_conflict_flags_winter_against_hot_warm():
    assert has_season_weather_conflict("Winter", "hot") is True
    assert has_season_weather_conflict("Winter", "warm") is True


def test_has_season_weather_conflict_not_for_cool_cold_rainy():
    assert has_season_weather_conflict("Winter", "cool") is False
    assert has_season_weather_conflict("Winter", "rainy") is False


def test_has_season_weather_conflict_false_when_missing_input():
    assert has_season_weather_conflict(None, "hot") is False
    assert has_season_weather_conflict("Winter", None) is False
