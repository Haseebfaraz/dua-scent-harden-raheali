"""Mirrors the real CustomerFragranceProfile shape from
app/services/customerProfile.server.js (emptyProfile()) -- one row per conversation, stored whole
in CustomerProfileState.profileJson. Field names match the JS camelCase exactly since the JSON
blob is shared verbatim between Node and Python against the same DB row.
"""

from pydantic import BaseModel, ConfigDict, Field

VALID_SEASONS = ["Winter", "Spring", "Summer", "Fall"]
VALID_STRENGTH_PREFERENCES = ["light", "moderate", "strong"]
VALID_WEATHER_DIRECTIONS = ["hot", "warm", "mild", "cool", "cold", "humid", "rainy"]
VALID_LOCATION_SOURCES = ["geocoding", "order_history", "customer_confirmed"]


class CurrentWeather(BaseModel):
    condition: str | None = None
    temperatureC: float | None = None
    fetchedAt: str | None = None


class WeatherLocation(BaseModel):
    city: str | None = None
    country: str | None = None
    verified: bool = False


class VocabularyCorrection(BaseModel):
    field: str
    original: str
    corrected: str


class CustomerFragranceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    email: str | None = None
    city: str | None = None
    stateRegion: str | None = None
    country: str | None = None
    requestedSeasonStyle: str | None = None
    seasonStyleConflictResolved: bool = False
    currentWeather: CurrentWeather = Field(default_factory=CurrentWeather)
    weatherDirection: str | None = None
    weatherLocation: WeatherLocation = Field(default_factory=WeatherLocation)
    likes: list[str] = Field(default_factory=list)
    dislikes: list[str] = Field(default_factory=list)
    preferenceVocabularyCorrections: list[VocabularyCorrection] = Field(default_factory=list)
    preferredStyle: str | None = None
    inferredStyle: str | None = None
    occasion: str | None = None
    giftRecipient: str | None = None
    dislikesAsked: bool = False
    occasionAsked: bool = False
    strengthPreference: str | None = None
    additionalPreferences: list[str] = Field(default_factory=list)
    locationVerified: bool = False
    locationSource: str | None = None
    selectedRecommendationId: str | None = None
    pendingRecreateRecommendationId: str | None = None
