"""The customer-safe boundary between private server data and anything a customer-facing
model (or the browser) may receive (Phase 3, findings F3 / N5 / N8).

Two typed, ALLOWLIST-built views live here:

  * CustomerSafeProfileView   -- what the conversational model needs in order not to re-ask
                                 questions. Built field by field from the stored profile; internal
                                 workflow ids, flags, capability data, and the email itself are
                                 never copied.
  * CustomerSafeRecommendation -- the only representation of a recommendation that may leave the
                                 private engine: fragrance name, character, note impressions,
                                 strength, occasion/climate fit, why it matches, a caveat, a
                                 categorical match label, and an availability state. Source
                                 product identities, handles, SKUs, scores, order-history evidence,
                                 inventory numbers, Shopify ids, and database ids are not fields
                                 of this model and cannot be smuggled in: `extra="forbid"` rejects
                                 unknown keys and the builders construct every field explicitly.

Every builder takes the private object and copies ONLY named fields. A new field added to the
internal recommendation next month therefore stays private by default.

Customer context reaches the model as DATA, not as system-prompt prose: `customer_context_messages`
returns a synthetic tool-call / tool-result pair (the OpenAI message shape designed for data) so
that customer-controlled strings never sit inside the trusted instruction text (N5).
"""

import json
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.customer_profile import get_missing_required_fields
from app.services.fragrance_build import compute_note_position_buckets

CUSTOMER_CONTEXT_TOOL_NAME = "load_customer_context"
RECOMMENDATION_TOOL_NAME = "present_fragrance_recommendation"
STATUS_TOOL_NAME = "fragrance_studio_status"

MAX_NOTES_PER_LAYER = 6
_MAX_TEXT = 400
_MAX_LIST_ITEMS = 20
_MAX_ITEM = 100

# Readiness dimensions the model may be told about, as short neutral labels (never the
# backend's own readiness sentences, which describe internal gating).
_MISSING_FIELD_LABELS = {
    "a fragrance direction": "style_or_vibe",
    "dislikes or hard exclusions": "dislikes",
    "occasion or use context": "occasion",
    "performance preference": "strength",
    "location": "location",
    "the customer's name": "name",
}


def _clean_text(value: Any, limit: int = _MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:limit] if cleaned else None


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item[:_MAX_ITEM] for item in (_clean_text(v, _MAX_ITEM) for v in value[:_MAX_LIST_ITEMS]) if item]


def missing_field_labels(profile: dict[str, Any]) -> list[str]:
    labels = []
    for sentence in get_missing_required_fields(profile):
        for prefix, label in _MISSING_FIELD_LABELS.items():
            if sentence.startswith(prefix):
                labels.append(label)
                break
    return labels


# ---------------------------------------------------------------------------
# Customer-safe profile view
# ---------------------------------------------------------------------------

class CustomerSafeProfileView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    emailKnown: bool = False
    likes: list[str] = Field(default_factory=list)
    dislikes: list[str] = Field(default_factory=list)
    preferredStyle: str | None = None
    occasion: str | None = None
    giftRecipient: str | None = None
    strength: str | None = None
    requestedSeasonStyle: str | None = None
    city: str | None = None
    country: str | None = None
    currentWeather: str | None = None
    climate: str | None = None
    additionalPreferences: list[str] = Field(default_factory=list)
    stillNeeded: list[str] = Field(default_factory=list)


def _without_instruction_like(profile: dict[str, Any]) -> dict[str, Any]:
    """Phase 4A (defense in depth): values stored BEFORE the write-time guard existed may read
    like instructions to the assistant. They are withheld from the MODEL projection only; the
    stored profile is untouched. Legitimate fragrance terms and unusual names pass
    (see security_gate.looks_like_instruction). This does not claim to catch every semantic
    injection; customer data also stays outside trusted instructions (N5)."""
    from app.ai.security_gate import looks_like_instruction

    cleaned: dict[str, Any] = {}
    for key, value in profile.items():
        if isinstance(value, str):
            cleaned[key] = None if looks_like_instruction(value) else value
        elif isinstance(value, list):
            cleaned[key] = [v for v in value if not (isinstance(v, str) and looks_like_instruction(v))]
        else:
            cleaned[key] = value
    return cleaned


def build_customer_safe_profile_view(profile: dict[str, Any]) -> CustomerSafeProfileView:
    profile = _without_instruction_like(profile or {})
    weather = profile.get("currentWeather") or {}
    return CustomerSafeProfileView(
        name=_clean_text(profile.get("name"), 100),
        emailKnown=bool(profile.get("email")),
        likes=_clean_list(profile.get("likes")),
        dislikes=_clean_list(profile.get("dislikes")),
        preferredStyle=_clean_text(profile.get("preferredStyle"), 200) or _clean_text(profile.get("inferredStyle"), 200),
        occasion=_clean_text(profile.get("occasion"), 200),
        giftRecipient=_clean_text(profile.get("giftRecipient"), 100),
        strength=_clean_text(profile.get("strengthPreference"), 20),
        requestedSeasonStyle=_clean_text(profile.get("requestedSeasonStyle"), 20),
        city=_clean_text(profile.get("city"), 100) if profile.get("locationVerified") else None,
        country=_clean_text(profile.get("country"), 100) if profile.get("locationVerified") else None,
        currentWeather=_clean_text(weather.get("condition") if isinstance(weather, dict) else None, 60),
        climate=_clean_text(profile.get("weatherDirection"), 20),
        additionalPreferences=_clean_list(profile.get("additionalPreferences")),
        stillNeeded=missing_field_labels(profile),
    )


def customer_context_messages(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """The customer context as a synthetic tool-call/result pair placed right after the static
    system prompt. Tool results are the API's data channel: nothing in here is an instruction."""
    call_id = f"ctx_{uuid.uuid4().hex[:12]}"
    view = build_customer_safe_profile_view(profile)
    return [
        {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {"name": CUSTOMER_CONTEXT_TOOL_NAME, "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": call_id, "content": json.dumps({"customerContext": view.model_dump()}, ensure_ascii=False)},
    ]


# ---------------------------------------------------------------------------
# Customer-safe recommendation
# ---------------------------------------------------------------------------

MatchLabel = Literal["strong match", "balanced match", "adventurous match"]
Availability = Literal["AVAILABLE", "AVAILABILITY_UNCONFIRMED", "UNAVAILABLE"]


class CustomerSafeNotes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    top: list[str] = Field(default_factory=list)
    middle: list[str] = Field(default_factory=list)
    base: list[str] = Field(default_factory=list)


class CustomerSafeRecommendation(BaseModel):
    """Everything a customer may learn about a recommendation. Nothing else exists on this
    type, and unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    name: str
    character: str | None = None
    whyItMatches: str | None = None
    bestFor: str | None = None
    climateFit: str | None = None
    strength: str | None = None
    caveat: str | None = None
    matchLabel: MatchLabel = "balanced match"
    notes: CustomerSafeNotes = Field(default_factory=CustomerSafeNotes)
    availability: Availability = "AVAILABILITY_UNCONFIRMED"


def match_label_for_confidence(confidence: Any) -> MatchLabel:
    if confidence in ("very high", "high"):
        return "strong match"
    if confidence == "medium":
        return "balanced match"
    return "adventurous match"


def availability_from_snapshot(snapshot: Any) -> Availability:
    """Collapse the private inventory snapshot into a customer-safe state. Missing mapping, SKU
    not found, lookup failures, and 'never checked' are all UNCONFIRMED -- never a number, never a
    reason."""
    if snapshot is None:
        return "AVAILABILITY_UNCONFIRMED"
    buildable = getattr(snapshot, "buildable", None)
    validated = getattr(snapshot, "inventoryValidated", None)
    if buildable is False:
        return "UNAVAILABLE"
    if buildable and validated:
        return "AVAILABLE"
    return "AVAILABILITY_UNCONFIRMED"


def _notes_from_internal_products(internal_products: Any, customer_likes: Any) -> CustomerSafeNotes:
    products = internal_products if isinstance(internal_products, list) else []
    # Note NAMES are part of the finished scent the customer sees on the preview page; the
    # product titles that carry them are not copied anywhere.
    buckets = compute_note_position_buckets([{"notes": p.get("notes") or []} for p in products if isinstance(p, dict)], customer_likes or [])
    return CustomerSafeNotes(
        top=_clean_list(buckets.get("top"))[:MAX_NOTES_PER_LAYER],
        middle=_clean_list(buckets.get("middle"))[:MAX_NOTES_PER_LAYER],
        base=_clean_list(buckets.get("base"))[:MAX_NOTES_PER_LAYER],
    )


def build_customer_safe_recommendation(record: Any, *, inventory_snapshot: Any = None, customer_facing: dict[str, Any] | None = None) -> CustomerSafeRecommendation:
    """From a FragranceRecommendation row (or any object with the same attributes). Only the named
    fields below are read; `productsJson` is consulted solely for note names."""
    facing = customer_facing if customer_facing is not None else (getattr(record, "customerFacingJson", None) or {})
    score_json = getattr(record, "scoreJson", None) or {}
    profile_json = getattr(record, "customerProfileJson", None) or {}
    return CustomerSafeRecommendation(
        name=_clean_text(getattr(record, "draftName", None), 80) or _clean_text(facing.get("customerFacingName"), 80) or "Custom Blend",
        character=_clean_text(facing.get("customerFacingDescription")),
        whyItMatches=_clean_text(facing.get("customerFacingWhySuits")),
        bestFor=_clean_text(facing.get("customerFacingBestUse")),
        climateFit=_clean_text(facing.get("customerFacingWeatherSuitability")),
        strength=_clean_text(facing.get("customerFacingStrength"), 20),
        caveat=_clean_text(facing.get("customerFacingRisk")),
        matchLabel=match_label_for_confidence(score_json.get("confidence")),
        notes=_notes_from_internal_products(getattr(record, "productsJson", None), profile_json.get("likes")),
        availability=availability_from_snapshot(inventory_snapshot),
    )


def build_customer_safe_recommendation_from_candidate(candidate: dict[str, Any], *, inventory: dict[str, Any] | None = None, likes: Any = None) -> CustomerSafeRecommendation:
    """Same allowlist, applied to an in-memory engine proposal (before/after persistence)."""
    availability: Availability = "AVAILABILITY_UNCONFIRMED"
    if inventory:
        if inventory.get("buildable") is False:
            availability = "UNAVAILABLE"
        elif inventory.get("buildable") and inventory.get("inventoryValidated"):
            availability = "AVAILABLE"
    return CustomerSafeRecommendation(
        name=_clean_text(candidate.get("customerFacingName"), 80) or "Custom Blend",
        character=_clean_text(candidate.get("customerFacingDescription")),
        whyItMatches=_clean_text(candidate.get("customerFacingWhySuits")),
        bestFor=_clean_text(candidate.get("customerFacingBestUse")),
        climateFit=_clean_text(candidate.get("customerFacingWeatherSuitability")),
        strength=_clean_text(candidate.get("customerFacingStrength"), 20),
        caveat=_clean_text(candidate.get("customerFacingRisk")),
        matchLabel=match_label_for_confidence(candidate.get("confidence")),
        notes=_notes_from_internal_products(candidate.get("internalProducts"), likes),
        availability=availability,
    )


def recommendation_presentation_messages(safe: CustomerSafeRecommendation) -> list[dict[str, Any]]:
    """The safe recommendation as a synthetic tool result for the bridge completion. No ids."""
    call_id = f"rec_{uuid.uuid4().hex[:12]}"
    return [
        {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {"name": RECOMMENDATION_TOOL_NAME, "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": call_id, "content": json.dumps({"recommendation": safe.model_dump()}, ensure_ascii=False)},
    ]


def status_messages(status: str, guidance: str) -> list[dict[str, Any]]:
    """A non-READY pipeline outcome as a synthetic tool result: a status label plus
    server-authored guidance. No reasons, ids, inventory, or scores."""
    call_id = f"sts_{uuid.uuid4().hex[:12]}"
    return [
        {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {"name": STATUS_TOOL_NAME, "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": call_id, "content": json.dumps({"status": status, "instruction": guidance}, ensure_ascii=False)},
    ]
