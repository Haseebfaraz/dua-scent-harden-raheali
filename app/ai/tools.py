"""Model-callable tools (Phase 3, finding F3): the customer-facing conversational model is an
untrusted caller with LEAST privilege.

Before Phase 3 the model could call 13 tools, including private catalog lookups by arbitrary
title, candidate analysis returning raw order-history evidence and scores, and the recommendation
generators. All of those are now SERVER-ONLY (app/services/recommendation_pipeline.py and
app/ai/tool_executor.py's private dispatcher); they are not advertised to the model, cannot be
named by it, and their results never enter model context.

What remains model-callable, and why each must stay model-visible:

  * save_customer_profile_field  -- the model is the only component that understands what the
                                    customer just said; it records structured preference facts.
                                    Field allowlist, typed values, bounded sizes; identity fields
                                    cannot override a known identity.
  * verify_customer_location     -- the customer types a city; verification (order history +
                                    geocoding) is server-side. The model only passes the text.
  * resolve_season_preference    -- a two-value enum answering a question the model asked.
  * refine_fragrance_recommendation -- the customer asks for a change in their own words after a
                                    recommendation exists; the private engine does the work and
                                    returns only the customer-safe result.

Every argument object is validated with a strict schema (unknown keys rejected, bounded strings,
enums). Initial recommendation generation is not a tool at all: the server runs the private
pipeline itself the moment the profile is complete.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.services.customer_profile import VALID_SEASONS, VALID_STRENGTH_PREFERENCES

PROFILE_FIELD_NAMES = [
    "name", "email", "city", "stateRegion", "country", "requestedSeasonStyle",
    "likes", "dislikes", "preferredStyle", "occasion", "giftRecipient",
    "dislikesAsked", "occasionAsked", "locationAsked", "nameAsked", "strengthPreference", "additionalPreferences",
    "fragrancePivotOffered", "fragrancePivotDeclined",
    "customBuildInvited", "customBuildAccepted", "customBuildDeclined",
]

# Field name -> ("string" | "string_array" | "boolean" | "enum", extra) -- mirrors
# PROFILE_FIELD_SCHEMAS' shape closely enough to validate without a full Zod-equivalent library.
PROFILE_FIELD_KINDS: dict[str, tuple[str, object]] = {
    "name": ("string", None),
    "email": ("email", None),
    "city": ("string", None),
    "stateRegion": ("string", None),
    "country": ("string", None),
    "requestedSeasonStyle": ("enum", VALID_SEASONS),
    "likes": ("string_array", None),
    "dislikes": ("string_array", None),
    "preferredStyle": ("string", None),
    "occasion": ("string", None),
    "giftRecipient": ("string", None),
    "dislikesAsked": ("boolean", None),
    "occasionAsked": ("boolean", None),
    "locationAsked": ("boolean", None),
    "nameAsked": ("boolean", None),
    "strengthPreference": ("enum", VALID_STRENGTH_PREFERENCES),
    "additionalPreferences": ("string_array", None),
    "fragrancePivotOffered": ("boolean", None),
    "fragrancePivotDeclined": ("boolean", None),
    "customBuildInvited": ("boolean", None),
    "customBuildAccepted": ("boolean", None),
    "customBuildDeclined": ("boolean", None),
}


def validate_profile_field_value(field: str, value: object) -> tuple[bool, object]:
    """Returns (True, coerced_value) or (False, error_message)."""
    kind, extra = PROFILE_FIELD_KINDS[field]
    if kind == "string":
        if not isinstance(value, str) or not (1 <= len(value) <= 200):
            return False, f'invalid value for field "{field}": must be a non-empty string up to 200 characters'
        return True, value
    if kind == "email":
        if not isinstance(value, str) or "@" not in value or len(value) > 254:
            return False, f'invalid value for field "{field}": must be a valid email'
        return True, value
    if kind == "enum":
        if value not in extra:
            return False, f'invalid value for field "{field}": must be one of {extra}'
        return True, value
    if kind == "boolean":
        if not isinstance(value, bool):
            return False, f'invalid value for field "{field}": must be a boolean'
        return True, value
    if kind == "string_array":
        if not isinstance(value, list) or len(value) > 20 or any(not isinstance(v, str) or not v or len(v) > 100 for v in value):
            return False, f'invalid value for field "{field}": must be an array of up to 20 non-empty strings of at most 100 characters'
        return True, value
    return False, f'invalid value for field "{field}"'


# ---------------------------------------------------------------------------
# Strict argument schemas for the model-callable tools (unknown keys are rejected)
# ---------------------------------------------------------------------------

class SaveProfileFieldArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: Literal[
        "name", "email", "city", "stateRegion", "country", "requestedSeasonStyle",
        "likes", "dislikes", "preferredStyle", "occasion", "giftRecipient",
        "dislikesAsked", "occasionAsked", "locationAsked", "nameAsked", "strengthPreference", "additionalPreferences",
        "fragrancePivotOffered", "fragrancePivotDeclined",
        "customBuildInvited", "customBuildAccepted", "customBuildDeclined",
    ]
    value: str | bool | list[str]


class VerifyLocationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cityText: str = Field(min_length=1, max_length=120)


class ResolveSeasonArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    choice: Literal["keep_style", "use_weather"]


class RefineRecommendationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feedback: str = Field(min_length=1, max_length=500)


MODEL_TOOL_ARG_SCHEMAS: dict[str, type[BaseModel]] = {
    "save_customer_profile_field": SaveProfileFieldArgs,
    "verify_customer_location": VerifyLocationArgs,
    "resolve_season_preference": ResolveSeasonArgs,
    "refine_fragrance_recommendation": RefineRecommendationArgs,
}


class ModelToolArgumentError(ValueError):
    pass


def validate_model_tool_arguments(tool_name: str, raw_args: Any) -> dict[str, Any]:
    schema = MODEL_TOOL_ARG_SCHEMAS.get(tool_name)
    if schema is None:
        raise ModelToolArgumentError(f'unknown tool "{tool_name}"')
    if not isinstance(raw_args, dict):
        raise ModelToolArgumentError("arguments must be a JSON object")
    try:
        return schema.model_validate(raw_args).model_dump()
    except ValidationError:
        raise ModelToolArgumentError(f"invalid arguments for {tool_name}") from None


# ---------------------------------------------------------------------------
# Tool schemas advertised to the model
# ---------------------------------------------------------------------------

FRAGRANCE_AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_customer_profile_field",
            "description": "Save one field of the customer's structured fragrance profile (name, email, city, stateRegion, country, requestedSeasonStyle, likes, dislikes, preferredStyle, occasion, giftRecipient, dislikesAsked, occasionAsked, locationAsked, nameAsked, strengthPreference, additionalPreferences, fragrancePivotOffered, fragrancePivotDeclined, customBuildInvited, customBuildAccepted, customBuildDeclined). Call this every time the customer gives you a real answer for one of these — never track profile progress in your own memory. A single message often supplies several of these at once (e.g. 'strong and woody for date night, I'm in LA, love oud and hate vanilla' touches strengthPreference, preferredStyle/likes, occasion, city, and dislikes) — save every field it actually contains, not just one. strengthPreference captures longevity/projection/strength -- save it whenever the customer describes how strong or long-lasting they want it, even said casually as part of a like ('I like it strong', 'nothing too loud', 'needs to last all day'), not only when asked directly as its own question; if your OWN previous message explicitly asked how strong/long-lasting they want it, a short reply like 'very' or 'not much' means exactly that in context ('very' -> strong) -- save it directly, do not ask them to repeat or clarify what you just asked. requestedSeasonStyle is ONLY for when the customer volunteers a specific seasonal style unprompted (e.g. 'I want something wintery') — never ask them what season it is or what season they associate with an occasion; live weather is handled automatically once their city is verified. giftRecipient is ONLY set when the customer indicates this is a gift for someone else (e.g. 'husband', 'wife', 'friend') — once set, likes/dislikes/preferredStyle/occasion describe that recipient, not necessarily the person chatting. dislikesAsked/occasionAsked/locationAsked/nameAsked are booleans (true/false) — set to true the moment you've asked about dislikes/occasion/location/name (or already knew the answer from earlier context), regardless of whether the real answer was 'none'/'nothing specific'/'prefer not to say' — an empty dislikes list, a null occasion, no city, or no name is ambiguous between 'never asked' and 'asked, declined to say', these flags disambiguate it so you never ask the same thing twice. If the customer's name genuinely isn't known yet (see the customer context for this), ask what you should call them at a natural low-signal moment and save the clear reply as 'name' immediately; do not interrupt a developing fragrance conversation just to ask for it, and if it doesn't come up naturally, set nameAsked to true instead of asking again. fragrancePivotOffered is a boolean you set to true in the SAME turn you introduce a soft fragrance bridge during general conversation (when FRAGRANCE PIVOT STATUS says DUE) — this is what lets the backend recognize the customer's next short reply ('yeah', 'sure', 'go ahead') as accepting that invitation. fragrancePivotDeclined is a boolean you set to true the moment the customer explicitly turns down that soft pivot or fragrance help in general. customBuildInvited is a boolean you set to true in the SAME turn you explicitly invite the customer to build a custom fragrance (when FRAGRANCE PIVOT STATUS says CUSTOM BUILD INVITATION DUE) — a bare style word like 'fruity' is fragrance interest, not the same thing as agreeing to a build, so save that preference and make this invitation before starting real discovery. customBuildAccepted and customBuildDeclined mirror the customer's actual answer to that invitation (or to a direct request they made themselves, like 'make me a fragrance') — once accepted, discovery is already underway; once declined, do not offer again unless the customer brings it up themselves.",
            "parameters": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": PROFILE_FIELD_NAMES, "description": "Which profile field to set."},
                    "value": {"description": "The value for this field. A plain string for most fields; an array of strings for likes/dislikes/additionalPreferences; a boolean (true/false) for dislikesAsked/occasionAsked/locationAsked/nameAsked/fragrancePivotOffered/fragrancePivotDeclined/customBuildInvited/customBuildAccepted/customBuildDeclined."},
                },
                "required": ["field", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_customer_location",
            "description": "Verify a city the customer typed — NEVER accept a city as real just because it sounds plausible (e.g. a fictional place). Call this the moment the customer gives a city, on that city text ALONE — the customer normally only needs to say the city; do not ask them for their country or state/region first. On success this automatically resolves the canonical city, state/region (when the place has one), country, and live weather, and saves all of it — you never separately ask for country or state after a successful verification, and you never ask the customer to repeat a city that already verified. needsClarification is RARE — it only becomes true when the location is genuinely indistinguishable (e.g. two comparably major, similarly-populated real places share the exact name). If needsClarification is true, ask the customer which real place they mean from the candidates. If verified is false, tell the customer you couldn't confidently match that location and ask for a real city. If the response says a style conflict needs confirming, ask the customer that ONE brief question before moving on; otherwise just continue naturally (e.g. into preferences/dislikes) without announcing that you looked anything up.",
            "parameters": {
                "type": "object",
                "properties": {"cityText": {"type": "string", "description": "The raw city text the customer gave."}},
                "required": ["cityText"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resolve_season_preference",
            "description": "Call this exactly once, only when a requestedSeasonStyle genuinely conflicted with the real weather and you've asked the customer which they want. choice='keep_style' keeps their requested style as the basis (e.g. 'a deeper winter-style character' even though it's warm out); choice='use_weather' bases it on today's real conditions instead, clearing the requested style. Never call this again once already resolved for this conversation.",
            "parameters": {
                "type": "object",
                "properties": {"choice": {"type": "string", "enum": ["keep_style", "use_weather"], "description": "Which direction the customer picked."}},
                "required": ["choice"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "refine_fragrance_recommendation",
            "description": "After a fragrance has already been created for this customer, use this when they ask for a change (e.g. 'make it sweeter', 'less musk', 'something fresher') — including after they return from the preview page asking to change it. Pass their request in their own words. The studio reworks the fragrance and hands you a short customer-safe summary of the new result, and the preview page opens on its own. After calling this, write ONE short, warm bridge that connects what they asked to change with the new result; do not list options or ask which they prefer.",
            "parameters": {
                "type": "object",
                "properties": {"feedback": {"type": "string", "description": "The customer's own refinement request, verbatim or closely paraphrased."}},
                "required": ["feedback"],
            },
        },
    },
]

MODEL_CALLABLE_TOOL_NAMES = [t["function"]["name"] for t in FRAGRANCE_AGENT_TOOLS]

# Offered instead of FRAGRANCE_AGENT_TOOLS while conversation_flow.py's determine_conversation_mode
# says GENERAL_CONVERSATION -- a deterministic guarantee, not just a prompt instruction: the model
# cannot verify locations or ask for refinements during small talk because those tools are not in
# the request at all. save_customer_profile_field stays available so a volunteered name/email can
# still be persisted.
GENERAL_CONVERSATION_TOOLS = [
    tool for tool in FRAGRANCE_AGENT_TOOLS if tool["function"]["name"] == "save_customer_profile_field"
]

# name/email/city/stateRegion/country are excluded: identity fields resolve elsewhere, and city
# must go through verify_customer_location (real geocoding), never a direct save -- callers of
# PROFILE_EXTRACTION_TOOL route a mentioned city through its separate cityText slot instead.
EXTRACTABLE_PROFILE_FIELD_NAMES = [
    "likes", "dislikes", "preferredStyle", "occasion", "giftRecipient",
    "requestedSeasonStyle", "strengthPreference", "additionalPreferences",
    "dislikesAsked", "occasionAsked", "locationAsked",
]

# A single dedicated, forced tool call that extracts every explicit high-confidence fact from one
# customer message at once, instead of the model spending one full round trip per field via
# repeated save_customer_profile_field calls (verified live: a single fact-dense message was
# producing 5+ sequential save_customer_profile_field round trips). conversation_flow.py calls the
# model with only this tool and tool_choice forced to it, persists everything deterministically in
# Python, then lets the normal tool loop continue from the now-updated profile -- so a customer
# revealing several facts at once no longer costs several sequential model round trips.
PROFILE_EXTRACTION_TOOL = {
    "type": "function",
    "function": {
        "name": "record_profile_updates",
        "description": (
            "Record every explicit, high-confidence fragrance-profile fact in the customer's latest "
            "message, all at once -- one message can supply several. Do NOT record anything ambiguous "
            "or uncertain (a bare color with no stated fragrance connection, vague small talk, a "
            "reading you're not confident in) -- leave it out entirely rather than guessing; the "
            "conversation will still ask about it naturally afterward. A genuine vibe/mood word "
            "(seductive, clean, bold, professional, comforting, mysterious, energetic, etc.) said "
            "about the fragrance itself IS explicit -- record it as a like/style value. "
            "strengthPreference covers longevity/projection/strength, including said casually "
            "('I like it strong'). If the customer states they have no dislikes, no particular "
            "occasion, or won't give a location, that is itself a fact -- set the matching *Asked "
            "flag to true rather than leaving it out. Never put a city here -- use cityText. If "
            "genuinely nothing new and explicit is in this message, call this with an empty "
            "fieldsToUpdate array and no cityText."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "fieldsToUpdate": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string", "enum": EXTRACTABLE_PROFILE_FIELD_NAMES},
                            "value": {"description": "A plain string for most fields; an array of strings for likes/dislikes/additionalPreferences; a boolean for the *Asked flags."},
                        },
                        "required": ["field", "value"],
                    },
                },
                "cityText": {"type": "string", "description": "The raw city the customer mentioned this message, verbatim, if any. Omit entirely if none."},
            },
            "required": ["fieldsToUpdate"],
        },
    },
}
