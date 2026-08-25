"""Port of app/tools/fragranceAgentTools.server.js's FRAGRANCE_AGENT_TOOLS (OpenAI function-
calling schema) and argument validation. Every handler validates its parsed arguments before
touching a service, and every handler talks to real data only.
"""

from app.services.customer_profile import VALID_SEASONS, VALID_STRENGTH_PREFERENCES

PROFILE_FIELD_NAMES = [
    "name", "email", "city", "stateRegion", "country", "requestedSeasonStyle",
    "likes", "dislikes", "preferredStyle", "occasion", "giftRecipient",
    "dislikesAsked", "occasionAsked", "locationAsked", "strengthPreference", "additionalPreferences",
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
    "strengthPreference": ("enum", VALID_STRENGTH_PREFERENCES),
    "additionalPreferences": ("string_array", None),
}


def validate_profile_field_value(field: str, value: object) -> tuple[bool, object]:
    """Returns (True, coerced_value) or (False, error_message)."""
    kind, extra = PROFILE_FIELD_KINDS[field]
    if kind == "string":
        if not isinstance(value, str) or not (1 <= len(value) <= 200):
            return False, f'invalid value for field "{field}": must be a non-empty string up to 200 characters'
        return True, value
    if kind == "email":
        if not isinstance(value, str) or "@" not in value:
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
        if not isinstance(value, list) or len(value) > 20 or any(not isinstance(v, str) or not v for v in value):
            return False, f'invalid value for field "{field}": must be an array of up to 20 non-empty strings'
        return True, value
    return False, f'invalid value for field "{field}"'


FRAGRANCE_AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "save_customer_profile_field",
            "description": "Save one field of the customer's structured fragrance profile (name, email, city, stateRegion, country, requestedSeasonStyle, likes, dislikes, preferredStyle, occasion, giftRecipient, dislikesAsked, occasionAsked, locationAsked, strengthPreference, additionalPreferences). Call this every time the customer gives you a real answer for one of these — never track profile progress in your own memory. A single message often supplies several of these at once (e.g. 'strong and woody for date night, I'm in LA, love oud and hate vanilla' touches strengthPreference, preferredStyle/likes, occasion, city, and dislikes) — save every field it actually contains, not just one. strengthPreference captures longevity/projection/strength -- save it whenever the customer describes how strong or long-lasting they want it, even said casually as part of a like ('I like it strong', 'nothing too loud', 'needs to last all day'), not only when asked directly as its own question. requestedSeasonStyle is ONLY for when the customer volunteers a specific seasonal style unprompted (e.g. 'I want something wintery') — never ask them what season it is or what season they associate with an occasion; live weather is handled automatically once their city is verified. giftRecipient is ONLY set when the customer indicates this is a gift for someone else (e.g. 'husband', 'wife', 'friend') — once set, likes/dislikes/preferredStyle/occasion describe that recipient, not necessarily the person chatting. dislikesAsked/occasionAsked/locationAsked are booleans (true/false) — set to true the moment you've asked about dislikes/occasion/location (or already knew the answer from earlier context), regardless of whether the real answer was 'none'/'nothing specific'/'prefer not to say' — an empty dislikes list, a null occasion, or no city is ambiguous between 'never asked' and 'asked, real answer was none', these flags disambiguate it so you never ask the same thing twice.",
            "parameters": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": PROFILE_FIELD_NAMES, "description": "Which profile field to set."},
                    "value": {"description": "The value for this field. A plain string for most fields; an array of strings for likes/dislikes/additionalPreferences; a boolean (true/false) for dislikesAsked/occasionAsked/locationAsked."},
                },
                "required": ["field", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_customer_profile",
            "description": "Get the customer's current structured fragrance profile and, if not yet ready, the single highest-value thing still missing before analysis can run. Discovery completeness requires ALL of: a style/vibe direction (likes, preferredStyle, inferredStyle, or additionalPreferences), dislikes or hard exclusions resolved (a real list, or dislikesAsked=true meaning the customer was asked and effectively said none), an occasion or use context resolved (occasion, or occasionAsked=true), a performance preference (strengthPreference), and location resolved (a verified city, or locationAsked=true meaning the customer was asked and couldn't/wouldn't give one). This is not a fixed question order and not a multiple-choice questionnaire -- one customer message can satisfy several of these at once -- but every one of them must be genuinely covered before analysis, not just one or two.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_customer_product_candidates",
            "description": "Deterministically score real DUA products against the customer's current profile using real order-history evidence (region, season/weather direction, likes/dislikes, repeat-purchase and popularity signals). Returns up to 10 real ProductCandidate results. Requires genuine discovery completeness first -- see get_customer_profile's description for the exact dimensions (style/vibe, dislikes-resolved, occasion-resolved, performance preference, location-resolved). A style direction plus only one other weak signal (e.g. 'strong oud' plus a passing mention of warm weather) is NOT enough on its own anymore -- calling this before every dimension is covered will fail with the specific thing still missing.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_product_notes_and_combination_status",
            "description": "Look up a real DUA product's exact notes, fragrance family, collection, and whether it's a single inspiration, an existing Hybrid/Tribrid/Quadbrid, or a component inside other existing combinations. Never infers notes from a title — only returns what's actually stored.",
            "parameters": {
                "type": "object",
                "properties": {"productTitle": {"type": "string", "description": "Exact or approximate real DUA product title."}},
                "required": ["productTitle"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_existing_combinations_for_product",
            "description": "Find every existing Hybrid/Tribrid/Quadbrid that either IS this product, or that USES this product as a component.",
            "parameters": {
                "type": "object",
                "properties": {"productTitle": {"type": "string", "description": "Exact or approximate real DUA product title."}},
                "required": ["productTitle"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_exact_combination_exists",
            "description": "Check whether a specific set of products already exists as an exact Hybrid/Tribrid/Quadbrid, regardless of the order given. Use this before proposing any new combination as 'new'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "productTitles": {
                        "type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 4,
                        "description": "The real DUA product titles making up the combination to check.",
                    },
                },
                "required": ["productTitles"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_combinations_using_similar_notes",
            "description": "Find existing combinations whose component products share real notes with the given product — useful evidence for 'combinations built from similar materials to this one'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "productTitle": {"type": "string", "description": "Exact or approximate real DUA product title."},
                    "limit": {"type": "number", "description": "Max results, default 5."},
                },
                "required": ["productTitle"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_customer_location",
            "description": "Verify a city the customer typed against real order-history data and real geocoding — NEVER accept a city as real just because it sounds plausible (e.g. a fictional place). Call this before saving city/country to the profile. If needsClarification is true, ask the customer which real place they mean from the candidates. If verified is false, tell the customer you couldn't confidently match that location and ask for a real city. On success, this AUTOMATICALLY fetches real live weather and derives a weatherDirection internally too — you never need a separate step for that, and you must never ask the customer what season it is or explain that you're adjusting anything 'accordingly'. If the response says a style conflict needs confirming, ask the customer that ONE brief question before moving on; otherwise just continue naturally (e.g. into preferences/dislikes).",
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
            "description": "Call this exactly once, only when a requestedSeasonStyle genuinely conflicted with the real weatherDirection and you've asked the customer which they want. choice='keep_style' keeps their requested style as the basis (e.g. 'a deeper winter-style character' even though it's warm out); choice='use_weather' bases it on today's real conditions instead, clearing the requested style. Never call this again once already resolved for this conversation.",
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
            "name": "select_recommendation",
            "description": "LEGACY/manual path only — generate_new_product_combinations now auto-selects and auto-confirms the best recommendation on its own, so you should not normally need this. Use it only if a customer is looking at an older message that actually listed multiple numbered combinations and picks one by number/phrase ('option 1', 'the first one', 'number 2'). Pass their message text through as-is — never try to figure out the recommendationId yourself from the product description.",
            "parameters": {
                "type": "object",
                "properties": {"selectionText": {"type": "string", "description": "The customer's own selection message, verbatim."}},
                "required": ["selectionText"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_new_product_combinations",
            "description": "Generate genuinely new Hybrid/Tribrid/Quadbrid combination proposals — built from the customer's top real product candidates plus compatible real supporting products — scored on preference match, seasonal fit, historical evidence, note compatibility, balance, and risk. NEVER proposes a combination that already exists. Call analyze_customer_product_candidates first in this conversation if you haven't yet. IMPORTANT: this tool automatically selects and confirms the single best-ranked recommendation for you and opens the fragrance preview page on its own (a preview_ready event) — it does NOT return a list for the customer to pick from. After calling this, do not list combinations, do not ask the customer to choose one, do not ask how they sound — just stop; the preview is already opening.",
            "parameters": {
                "type": "object",
                "properties": {
                    "maximumResults": {"type": "number", "description": "Default 8."},
                    "allowedTypes": {
                        "type": "array", "items": {"type": "string", "enum": ["HYBRID", "TRIBRID", "QUADBRID"]},
                        "description": "Restrict to specific combination types, e.g. if the customer asks for 'a Hybrid only'.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "refine_combination_recommendations",
            "description": "Re-generate combinations based on the customer's feedback (e.g. 'make it sweeter', 'remove the dark chocolate', 'give me a Hybrid only') — used after Recreate asks 'What would you like to change?', or any other time the customer wants an existing recommendation adjusted. Just like generate_new_product_combinations, this automatically selects and confirms the best refined result and opens the fragrance preview page on its own — it does NOT return a list for the customer to pick from. After calling this, do not list combinations, do not ask the customer to choose one — just stop; the preview is already opening.",
            "parameters": {
                "type": "object",
                "properties": {"feedback": {"type": "string", "description": "The customer's own refinement request, verbatim or closely paraphrased."}},
                "required": ["feedback"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_product_combination",
            "description": "LEGACY/manual path only — generate_new_product_combinations now auto-confirms the best recommendation on its own, so you should not normally need this. Use it only after select_recommendation resolved a customer's manual pick on an older conversation. Deterministically re-verifies everything (products still exist, notes exist, combination is still genuinely new, ratios sum to 100%, no high-severity dislike conflict). This does NOT create a Shopify product — it opens the fragrance preview page instead, where the customer can adjust it and explicitly choose to save or buy — never tell the customer a product has been created at this point.",
            "parameters": {
                "type": "object",
                "properties": {"recommendationId": {"type": "string", "description": "The exact recommendationId of the one specific combination the customer confirmed."}},
                "required": ["recommendationId"],
            },
        },
    },
]

# Offered instead of FRAGRANCE_AGENT_TOOLS while conversation_flow.py's determine_conversation_mode
# says GENERAL_CONVERSATION -- a deterministic guarantee, not just a prompt instruction: the model
# cannot call analyze_customer_product_candidates/generate_new_product_combinations during small
# talk because they are not in the request at all, and it never sees their fragrance-oriented tool
# descriptions during that phase either. save_customer_profile_field stays available so a
# volunteered name/email can still be persisted.
GENERAL_CONVERSATION_TOOLS = [
    tool for tool in FRAGRANCE_AGENT_TOOLS if tool["function"]["name"] == "save_customer_profile_field"
]
