"""Port of app/utils/weatherSeason.js -- deterministic weather/season helpers, pure functions.

The calendar season computed here is NEVER persisted to CustomerProfileState -- it's only an
ephemeral fallback (a ranking/query hint, or ice-breaker small talk).
"""

from datetime import datetime

SEASON_BY_MONTH = [
    "Winter", "Winter", "Spring", "Spring", "Spring", "Summer",
    "Summer", "Summer", "Fall", "Fall", "Fall", "Winter",
]

SOUTHERN_HEMISPHERE_COUNTRIES = {
    "australia", "new zealand", "argentina", "chile", "south africa", "brazil",
    "uruguay", "paraguay", "bolivia", "peru", "zimbabwe", "namibia", "botswana",
    "fiji", "madagascar",
}

_FLIP = {"Winter": "Summer", "Summer": "Winter", "Spring": "Fall", "Fall": "Spring"}


def get_calendar_season(country_name: str | None) -> str:
    month_index = datetime.now().month - 1
    northern_season = SEASON_BY_MONTH[month_index]
    if country_name and country_name.strip().lower() in SOUTHERN_HEMISPHERE_COUNTRIES:
        return _FLIP[northern_season]
    return northern_season


WMO_WEATHER_DESCRIPTIONS = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy with rime",
    51: "light drizzle", 53: "drizzle", 55: "dense drizzle",
    56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light rain showers", 81: "rain showers", 82: "violent rain showers",
    85: "light snow showers", 86: "snow showers",
    95: "a thunderstorm", 96: "a thunderstorm with light hail", 99: "a thunderstorm with heavy hail",
}

_CONDITION_WORD_BY_CODE = {
    0: "sunny", 1: "sunny", 2: "cloudy", 3: "cloudy",
    45: "cloudy", 48: "cloudy",
    51: "rainy", 53: "rainy", 55: "rainy", 56: "rainy", 57: "rainy",
    61: "rainy", 63: "rainy", 65: "rainy", 66: "rainy", 67: "rainy",
    71: "rainy", 73: "rainy", 75: "rainy", 77: "rainy",
    80: "rainy", 81: "rainy", 82: "rainy",
    85: "rainy", 86: "rainy",
    95: "windy", 96: "windy", 99: "windy",
}

_RAIN_CODES = {code for code, word in _CONDITION_WORD_BY_CODE.items() if word == "rainy"}
_HUMID_MIN_TEMP_F = 65
_HUMID_MIN_PERCENT = 60


def _temperature_word(temp_f: float) -> str:
    if temp_f >= 85:
        return "hot"
    if temp_f <= 40:
        return "chilly"
    if temp_f <= 60:
        return "cool"
    return "mild"


def describe_weather_simple(temp_f: float, weather_code: int) -> dict:
    condition_word = _CONDITION_WORD_BY_CODE.get(weather_code, "mild")
    temp_word = _temperature_word(temp_f)
    words = list(dict.fromkeys([temp_word, condition_word]))
    return {"words": words, "summary": " and ".join(words)}


def derive_weather_direction(
    temp_f: float, weather_code: int, relative_humidity_percent: float | None = None
) -> str:
    if weather_code in _RAIN_CODES:
        return "rainy"
    if (
        relative_humidity_percent is not None
        and relative_humidity_percent >= _HUMID_MIN_PERCENT
        and temp_f >= _HUMID_MIN_TEMP_F
    ):
        return "humid"
    if temp_f >= 85:
        return "hot"
    if temp_f >= 70:
        return "warm"
    if temp_f >= 55:
        return "mild"
    if temp_f >= 40:
        return "cool"
    return "cold"


def weather_direction_to_query_season(weather_direction: str | None, calendar_fallback: str) -> str:
    if weather_direction in ("hot", "warm", "humid"):
        return "Summer"
    if weather_direction in ("cold", "cool"):
        return "Winter"
    return calendar_fallback


_SEASON_STYLE_CONFLICT_DIRECTIONS = {
    "Winter": {"hot", "warm", "humid"},
    "Summer": {"cold", "cool"},
    "Spring": {"hot"},
    "Fall": {"hot"},
}


def has_season_weather_conflict(
    requested_season_style: str | None, weather_direction: str | None
) -> bool:
    if not requested_season_style or not weather_direction:
        return False
    conflict_directions = _SEASON_STYLE_CONFLICT_DIRECTIONS.get(requested_season_style)
    if not conflict_directions:
        return False
    return weather_direction in conflict_directions
