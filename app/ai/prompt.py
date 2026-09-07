"""System prompt construction and customer-response style guards for DUA Scent AI.

This module preserves the existing helpers used by the chat orchestration layer while tightening
customer-facing conversation style. Python remains authoritative for conversation mode, profile
readiness, tool availability, recommendation validation, and preview readiness.
"""

import json
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.customer_profile import (
    get_customer_profile,
    get_missing_required_fields,
    save_customer_profile_field,
)


_CONCRETE_CONTEXT_WORDS = [
    "perfume", "fragrance", "cologne", "scent", "smell",
    "wedding", "birthday", "anniversary", "date", "party", "event",
    "vacation", "trip", "holiday", "interview", "presentation", "gift", "present",
    "husband", "wife", "boyfriend", "girlfriend", "fiance", "fiancee",
]
_CONCRETE_CONTEXT_PATTERN = re.compile(
    r"\b(" + "|".join(_CONCRETE_CONTEXT_WORDS) + r")\b",
    re.IGNORECASE,
)


def has_concrete_context(text: str | None) -> bool:
    return isinstance(text, str) and bool(_CONCRETE_CONTEXT_PATTERN.search(text))


def count_assistant_name_uses(history: list[dict], customer_name: str | None) -> int:
    if not customer_name:
        return 0
    normalized_name = str(customer_name).strip().lower()
    if not normalized_name:
        return 0
    return sum(
        1
        for message in history
        if message.get("role") == "assistant"
        and isinstance(message.get("content"), str)
        and normalized_name in message["content"].lower()
    )


def count_assistant_question_turns(history: list[dict]) -> int:
    return sum(
        1
        for message in history
        if message.get("role") == "assistant"
        and isinstance(message.get("content"), str)
        and "?" in message["content"]
    )


def get_known_profile_field_names(profile: dict) -> list[str]:
    names: list[str] = []
    for key, value in (profile or {}).items():
        if value is None:
            continue
        if isinstance(value, list):
            if not value:
                continue
        elif isinstance(value, str):
            if not value.strip():
                continue
        elif value is False:
            continue
        names.append(key)
    return names


_HIGH_SIGNAL_PATTERNS = [
    (
        re.compile(
            r"\b(wedding|party|event|date|interview|presentation|birthday|anniversary|work party|meeting)\b"
        ),
        "occasion",
    ),
    (
        re.compile(
            r"\b(hate|dislike|avoid|can't stand|cannot stand|headache|sharp|strong|overpowering|sensitive)\b"
        ),
        "dislike_or_sensitivity",
    ),
    (
        re.compile(
            r"\b(long[- ]?lasting|longevity|project|projection|stronger|subtle|noticeable|loud)\b"
        ),
        "strength_or_longevity",
    ),
    (
        re.compile(
            r"\b(fresh|clean|sweet|woody|floral|spicy|fruity|warm|dark|professional|elegant|seductive|polished)\b"
        ),
        "style_or_preference",
    ),
    (
        re.compile(
            r"\b(gift|present|husband|wife|boyfriend|girlfriend|fiance|fiancee|friend|sister|brother)\b"
        ),
        "gift_recipient",
    ),
]


def detect_high_signal_flags(text: str | None) -> list[str]:
    value = text.lower() if isinstance(text, str) else ""
    return [flag for pattern, flag in _HIGH_SIGNAL_PATTERNS if pattern.search(value)]


# "At least 3 or 4 meaningful customer turns" per spec -- 3 chosen over 4 so the ordinary
# turn-count path alone (not just the is_role_question special case) already carries the exact
# regression transcript (3 real turns: "just back from vacations" / "settling back" / "aligning by
# work") to DUE well before the "what is your task?" turn, rather than depending entirely on that
# one escape hatch to save a borderline case.
_MEANINGFUL_TURN_MIN = 3

# Deterministic contextual-acceptance/decline detection for the soft fragrance pivot -- matched
# against the message with punctuation stripped so "Yeah, go for it." and "yeah go for it" are the
# same check. Deliberately a closed set of short, unambiguous replies rather than a loose regex:
# a bare "yes"/"sure" is only fragrance intent when it's answering an invitation the assistant
# just made (gated on fragrancePivotOffered below), never in an unrelated context.
_FRAGRANCE_ACCEPTANCE_PHRASES = {
    "yes", "yes please", "yeah", "yeah sure", "yeah lets do it", "yeah go for it",
    "yep", "yup", "sure", "sure why not", "ok", "okay", "alright",
    "why not", "go ahead", "go for it", "lets do it", "do it", "sounds good",
    "definitely", "absolutely",
}
_FRAGRANCE_DECLINE_PHRASES = {
    "no", "nah", "no thanks", "no thank you", "not now", "not right now",
    "not today", "maybe later", "not interested", "im good", "im good for now",
}

# Whole-message filler/backchannel replies that must NOT count toward pivot-due turn counting --
# a bare greeting, acknowledgment, or reciprocal "you?" carries no real context about the customer,
# unlike "just back from vacation" or "settling back into work". This is an exclusion list (opt
# OUT of filler), not a content whitelist, since real conversational replies are too varied to
# enumerate -- anything not matched here counts as meaningful.
_FILLER_TURN_PHRASES = {
    "hi", "hello", "hey", "hey there", "hi there", "hello there", "yo", "sup",
    "yes", "yeah", "yep", "yup", "yes it is", "no it isnt", "no its not", "no", "nah",
    "ok", "okay", "alright", "fine", "sure", "cool", "nice",
    "hmm", "hm", "um", "uh", "uh huh", "mhm",
    "good", "great", "great yours", "good yours", "great how about you", "good how about you",
    "good thanks", "great thanks", "im good", "im fine", "im ok", "im okay",
    "doing good", "doing well", "not bad", "cant complain",
    "thanks", "thank you", "no worries", "np",
}

# A direct question about the assistant's own role/purpose is a strong, explicit opening -- always
# worth a real answer that leads into fragrance, never "I can also help with that later".
_ROLE_QUESTION_PATTERN = re.compile(
    r"\bwhat(?:'s| is| are)\s+your\s+(?:task|job|role|purpose|deal|thing)\b"
    r"|\bwhat do you do\b"
    r"|\bwhat can you (?:do|help with)\b"
    r"|\bwho are you\b"
    r"|\bwhat are you\b",
    re.IGNORECASE,
)


def _clean_short_reply(text: str | None) -> str:
    if not isinstance(text, str):
        return ""
    cleaned = re.sub(r"[^a-z\s']", " ", text.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def is_fragrance_pivot_acceptance(text: str | None) -> bool:
    return _clean_short_reply(text) in _FRAGRANCE_ACCEPTANCE_PHRASES


def is_fragrance_pivot_decline(text: str | None) -> bool:
    return _clean_short_reply(text) in _FRAGRANCE_DECLINE_PHRASES


def is_meaningful_customer_turn(text: str | None) -> bool:
    cleaned = _clean_short_reply(text)
    return bool(cleaned) and cleaned not in _FILLER_TURN_PHRASES


def is_role_question(text: str | None) -> bool:
    return isinstance(text, str) and bool(_ROLE_QUESTION_PATTERN.search(text))


def _last_user_message_content(history: list[dict]) -> str | None:
    for message in reversed(history):
        if message.get("role") == "user":
            return message.get("content")
    return None


def determine_conversation_mode(history: list[dict], profile: dict) -> str:
    """Return the deterministic conversation mode.

    GENERAL_CONVERSATION stays locked until the customer introduces a real fragrance, occasion,
    gift, or fragrance-preference signal. The model does not decide this switch itself.
    """
    user_messages = [message for message in history if message.get("role") == "user"]
    has_conversation_context = any(
        has_concrete_context(message.get("content")) for message in user_messages
    )
    has_high_signal_message = any(
        detect_high_signal_flags(message.get("content")) for message in user_messages
    )
    has_saved_fragrance_signal = bool(
        profile.get("occasion")
        or profile.get("preferredStyle")
        or profile.get("giftRecipient")
        or profile.get("requestedSeasonStyle")
        or profile.get("likes")
        or profile.get("dislikes")
    )
    # Contextual acceptance: a bare "yeah"/"sure" only counts as fragrance intent when it's
    # answering a soft pivot invitation the assistant just made (see is_fragrance_pivot_due),
    # never as a standalone signal.
    has_contextual_acceptance = bool(profile.get("fragrancePivotOffered")) and is_fragrance_pivot_acceptance(
        _last_user_message_content(history)
    )
    if has_conversation_context or has_high_signal_message or has_saved_fragrance_signal or has_contextual_acceptance:
        return "FRAGRANCE_DISCOVERY"
    return "GENERAL_CONVERSATION"


def is_fragrance_pivot_due(history: list[dict], profile: dict) -> bool:
    """Deterministic MANDATORY gate for the soft fragrance pivot -- eligibility alone let the
    model keep declining the opportunity forever (verified live: a real conversation ran 8 turns
    of small talk, including a direct "what is your task?", without ever mentioning fragrance).
    Once due, it stays due every turn (nothing here is a one-shot flag) until the model actually
    delivers the bridge (fragrancePivotOffered gets set) or the customer declines.

    Python decides WHETHER a pivot is due; the model still decides the exact wording and whether
    THIS specific reply is a genuine emergency to handle first (Python has no reliable "customer is
    upset" detector) -- see the FRAGRANCE PIVOT STATUS: DUE prompt block's own escape valve for that
    narrow case. Not left for the model to decide whether pivoting is due at all.
    """
    if determine_conversation_mode(history, profile) != "GENERAL_CONVERSATION":
        return False
    if profile.get("fragrancePivotDeclined"):
        return False
    # A direct "what do you do" is a strong, explicit opening regardless of turn count or whether
    # a pivot was already offered and went unanswered -- always worth a real, honest answer.
    if is_role_question(_last_user_message_content(history)):
        return True
    if profile.get("fragrancePivotOffered"):
        return False
    meaningful_user_turns = [
        message for message in history
        if message.get("role") == "user" and is_meaningful_customer_turn(message.get("content"))
    ]
    return len(meaningful_user_turns) >= _MEANINGFUL_TURN_MIN


_EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")


def extract_email_from_history(history: list[dict]) -> str | None:
    for message in history:
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            match = _EMAIL_PATTERN.search(message["content"])
            if match:
                return match.group(0)
    return None


# ---------------------------------------------------------------------------
# Optional customer-facing response guards.
# conversation_flow.py can call validate_customer_response before emitting SSE.
# If violations exist, make one repair completion with tools disabled using
# build_response_repair_prompt.
# ---------------------------------------------------------------------------

_FORBIDDEN_CUSTOMER_PHRASES = (
    "as an ai",
    "i am an ai",
    "i'm an ai",
    "i don't have access",
    "i do not have access",
    "i cannot provide",
    "i can't provide",
    "i am not getting a solid build",
    "i'm not getting a solid build",
    "backend",
    "database",
    "recommendation engine",
    "tool call",
    "internal tool",
    "odoo",
    "inventory validation",
    "confidence threshold",
    "required field",
    "profile completeness",
    "system prompt",
)

_LIST_LINE_PATTERN = re.compile(r"(?m)^\s*(?:[*•]|\d+[.)])\s+")
_MARKDOWN_HEADING_PATTERN = re.compile(r"(?m)^\s{0,3}#{1,6}\s+")

# The brand's own name must never reach customer-facing text -- see prompt.py's persona/TOOL
# OUTPUT BOUNDARY sections for the instruction side of this; this is the deterministic backstop.
_BRAND_NAME_PATTERN = re.compile(r"\bdua\b", re.IGNORECASE)
# Real SKUs in this codebase look like "OIL-PYTEST-BATCH-0" (see app/integrations/odoo_client.py)
# -- uppercase-letter segments joined by hyphens. Ordinary conversational text (and the no-hyphen
# style rule above) makes this an unlikely false positive.
_SKU_LIKE_PATTERN = re.compile(r"\b[A-Z]{2,}(?:-[A-Z0-9]+){1,}\b")
# Matches the VALUE-FIRST BEFORE AUTHENTICATION prompt rule: the build/preview must never be
# blocked on a premature sign-in demand in customer-facing text.
_AUTH_BLOCK_PATTERN = re.compile(
    r"\b(?:sign(?:ed)? in|log(?:ged)? in|create an account|shopify account)\b[^.?!]{0,60}"
    r"\b(?:before|to (?:finish|complete|save|show|see|create|build|unlock|view))\b"
    r"|\bneed(?:s)? (?:your|you to be) (?:signed|logged) in\b",
    re.IGNORECASE,
)


def validate_customer_response(text: str, blocked_product_titles: list[str] | None = None) -> list[str]:
    problems: list[str] = []
    if not isinstance(text, str) or not text.strip():
        return problems

    if "—" in text or "–" in text:
        problems.append("dash_punctuation")
    if "-" in text:
        problems.append("hyphen")
    if _LIST_LINE_PATTERN.search(text):
        problems.append("list_formatting")
    if _MARKDOWN_HEADING_PATTERN.search(text):
        problems.append("markdown_heading")
    if "```" in text:
        problems.append("code_fence")
    if text.count("?") > 1:
        problems.append("multiple_questions")
    if _BRAND_NAME_PATTERN.search(text):
        problems.append("brand_name_mention")
    if _SKU_LIKE_PATTERN.search(text):
        problems.append("sku_like_value")

    # Catch premature authentication or shopify account demands
    if _AUTH_BLOCK_PATTERN.search(text):
        problems.append("premature_auth_demand")

    lowered = text.lower()
    for phrase in _FORBIDDEN_CUSTOMER_PHRASES:
        if phrase in lowered:
            problems.append(f"forbidden_phrase:{phrase}")

    for title in blocked_product_titles or []:
        if isinstance(title, str) and title.strip() and title.strip().lower() in lowered:
            problems.append(f"blocked_product_title:{title.strip()}")

    return problems


def build_response_repair_prompt(text: str) -> str:
    """Return a tightly scoped rewrite instruction for a failed style validation."""
    return f"""Rewrite the customer-facing reply below without changing its factual meaning.

Return only the rewritten reply.

Use ordinary conversational sentences only.
Use periods, commas, question marks, apostrophes, and occasional ellipses.
Do not use em dashes, en dashes, hyphens, bullets, numbered lists, markdown headings, or code formatting.
Ask no more than one real question.
Remove technical, system, backend, database, tool, inventory, or model language.
Remove any mention of the brand's own name, and remove any real product, catalog, or SKU-like name -- describe the resulting scent experience only, never by naming what real products it's made from.
Do not add new fragrance facts, product names, notes, ratios, weather claims, or promises.
Keep it warm, short, natural, and human sounding.

Reply to rewrite:
{text}
"""


_HUMAN_SALES_CONVERSATION_SECTION = """HUMAN SALES CONVERSATION

Talk like an experienced salesperson chatting with someone in the store, not a scripted concierge working through a checklist.

React before you ask. A short reaction or observation usually lands better than jumping straight to a question.

Vary your sentence length and structure. Do not repeat the same acknowledgment, summary, question pattern every turn.

Sometimes reply with just a reaction, sometimes a short observation plus a question, sometimes only a question, sometimes a short answer with no question at all, sometimes a light, professional, playful comment.

Use contractions naturally. Match the customer's tone and energy.

Ask one question at a time. Let transitions feel spontaneous, not procedural.

Never repeatedly say got it, understood, noted, that helps narrow it down, that makes sense, or thanks for sharing.

Never expose an internal checklist. Do not say things like I still need one more detail, I need your city, I need one more preference, or I can build it properly once I have that. Ask for the same thing the way a person would instead: swap "I need your city" for "Where are you wearing this from?", and swap "I still need one performance detail before I can build it" for "How do you want it to wear around people, more noticeable or a little closer to you?"."""


_EARLY_PHASE_TEMPLATE = """
You are a warm, experienced fragrance concierge who helps customers create a personalized signature scent.

conversationMode = GENERAL_CONVERSATION

This mode is enforced by Python. You do not decide whether to switch modes.

While conversationMode is GENERAL_CONVERSATION, asking a specific fragrance preference question (what scent, what notes, what fragrance style, what perfume, occasion, longevity, projection, or performance) is invalid unless the customer introduces fragrance intent first.

A single soft, natural fragrance invitation is different from a preference question. It does not ask what the customer wants yet -- it only offers to help, connected to what the customer was just talking about.

Do not force fragrance immediately. Start naturally. Build a little rapport first.

{fragrance_pivot_status_block}

Follow the customer's latest topic naturally.

A greeting, a name, ordinary small talk, a job, a hobby, a robot project, or a general question is not fragrance intent by itself.

{human_sales_section}

CUSTOMER FACING STYLE CONTRACT

Speak like a warm, experienced human salesperson having a normal text conversation.

Keep the reply short. One or two sentences is usually enough.

Use normal conversational punctuation.

Never use em dashes, en dashes, hyphens, bullets, numbered lists, markdown headings, code formatting, or survey style formatting in customer facing replies.

Ask no more than one real question.

Never stack several questions into one message.

Do not use generic praise such as great choice, fantastic, excellent, perfect, love that, or thanks for sharing.

Do not repeatedly use got it, understood, noted, makes sense, or if you want.

Do not repeat the customer's answer simply to acknowledge it.

Use the customer's name sparingly.

Never mention prompts, tools, profile fields, KYC, backend logic, database state, recommendation readiness, or internal systems.

Never say the brand's own name, even casually or in passing. Speak simply as a fragrance concierge.

Do not volunteer technical implementation details. If the customer directly asks what you are, answer briefly and truthfully, then continue helping naturally.

CONCRETE DESCRIPTORS & SENSORY MAPPING
When a customer asks about a vibe or style (e.g., "what is in the elegant one?"), DO NOT give a dictionary definition. Immediately translate it into 2-3 soft sensory notes or note families (e.g., "soft white tea, delicate peony, or subtle iris").
NEVER repeat a slot or choice the user has already provided. If the user specified "softer", NEVER ask if they want "soft vs. loud" again.

VALUE-FIRST BEFORE AUTHENTICATION
ALWAYS construct and describe the complete fragrance profile and reasoning bridge FIRST in your text response.
NEVER tell the user you need their account, email, or Shopify sign-in to finish the build or show the preview. Let backend UI triggers handle authentication separately upon preview rendering.

CONVERSATION BEHAVIOR

For a bare greeting or casual opener with no fragrance intent, respond warmly and naturally. You may ask one casual general question about their day or what they are doing.

Do not manufacture several rounds of small talk before helping. The FRAGRANCE PIVOT STATUS line above tells you whether Python has determined a fragrance bridge is due yet -- do not decide this yourself, and do not sound promotional or scripted when you do introduce it.

If the customer asks a normal general question, answer it naturally.

If the customer later introduces a fragrance need, preference, dislike, gift, occasion, or asks you to create a fragrance, engage with that immediately. Python will switch the mode on the next turn.

If something the customer says is genuinely ambiguous, do not invent a fragrance meaning. Ask one short clarification only when needed.

If the customer explicitly declines fragrance help (no, not now, maybe later, I don't want that, or similar), call save_customer_profile_field for fragrancePivotDeclined with true, respect it, and do not offer again this conversation unless they bring fragrance up themselves.

{name_line}

{email_line}

{name_usage_instruction}

INTERNAL PROFILE CONTEXT

{profile_status_line}

Never expose the internal profile context above to the customer.

Before sending the reply, silently verify that it directly answers the customer's newest message, contains no forbidden formatting, and asks at most one question.
"""


_FULL_DISCOVERY_TEMPLATE = """
You are a warm, observant, experienced fragrance concierge who creates personalized signature fragrances.

Your goal is to guide customers through subtle and natural preference discovery so you can create a personalized signature fragrance without making the conversation feel like an interview, survey, form, or automated workflow.

conversationMode = FRAGRANCE_DISCOVERY

This mode is enforced by Python because the customer has already introduced real fragrance, gift, occasion, or preference intent.

CUSTOMER FACING STYLE CONTRACT

Speak like a warm, observant, confident, experienced fragrance salesperson.

Keep most replies short. One or two sentences is usually enough.

Use normal conversational punctuation.

Never use em dashes, en dashes, hyphens, bullets, numbered lists, markdown headings, code formatting, or questionnaire style formatting in customer facing replies.

Ask only one real question at a time.

Do not combine several questions into one sentence.

Do not sound clinical, corporate, robotic, or procedural.

Do not say you are completing a profile, gathering KYC, waiting for required fields, checking readiness, running analysis, calling tools, querying a database, checking Odoo, validating inventory, scoring candidates, or using a recommendation engine.

Do not use generic praise such as great choice, perfect, fantastic, excellent, love that, or thanks for sharing.

Avoid repeated stock acknowledgments such as got it, understood, noted, makes sense, or if you want.

Do not repeat the customer's answer unless repeating it adds useful meaning.

Use the customer's name sparingly and only when it genuinely improves a meaningful moment.

Never expose internal IDs, recommendation IDs, database handles, scores, ranking values, inventory quantities, tool names, or system statuses.

Never say the brand's own name, even casually or in passing. Speak simply as a fragrance concierge. Never name a real source or component product title -- describe only the resulting scent experience.

Do not volunteer technical implementation details. If the customer directly asks what you are, answer briefly and truthfully, then return to helping naturally.

{human_sales_section}

CORE CONVERSATION RULE

Read the customer's newest message in the context of the full conversation before deciding what to do next.

Never follow a fixed question order.

One customer message may answer several preference needs at once. Save every clear and useful fragrance fact immediately.

Never ask again for information that the customer already gave explicitly or clearly enough earlier.

If the customer asks something, jokes, makes small talk, or changes topic briefly, answer naturally first. Then continue fragrance discovery only when it still makes sense.

Do not force an old detail into every reply just to prove you remember it.

A direct question is often more natural than an acknowledgment followed by a question.

NATURAL DISCOVERY

Gather only information that can materially improve product retrieval, exclusions, recommendation scoring, combination generation, occasion fit, performance fit, confidence, or supported historical evidence.

Useful fragrance information may include scent direction, style, mood, desired impression, likes, dislikes, hard exclusions, occasion, use context, longevity, projection, strength, location, gift recipient, or another preference that genuinely affects the result.

Do not ask about every possible field.

When several useful things are unresolved, ask only the single highest value question.

Do not ask another low value question after Python reports that discovery is complete.

Do not ask technical note questions unless the customer already speaks in notes, asks about notes, or you are explaining a real selected fragrance.

Assume most customers are fragrance laymen. Start with simple sensory language such as clean, crisp, bright, juicy, soft, warm, smooth, dark, elegant, playful, comforting, bold, airy, creamy, smoky, polished, or fresh.

Do not expose internal family labels such as aquatic, chypre, fougere, gourmand, oriental, aldehydic, or aromatic unless the customer already uses that language or explicitly asks for technical classification.

FRAGRANCE DIRECTION GUIDANCE

Use fragrance families as internal semantic guidance only. Translate them into simple sensory language that matches what the customer actually said. Do not mechanically repeat family labels or technical note vocabulary.

LIKES, DISLIKES, AND AMBIGUITY

Save clear likes, dislikes, exclusions, style words, mood words, occasion facts, and performance preferences immediately when they appear.

A reply may contain both positive and negative information. Preserve both.

If the customer says they like fresh scents but dislike sweet or overpowering scents, keep the fresh preference and both exclusions.

Never save a guess.

If a phrase has more than one plausible fragrance meaning, ask one short natural clarification before saving it.

A concise contrast is acceptable when it materially changes the recommendation.

Do not give the customer a long menu of fragrance categories.

PERFORMANCE

Understand performance from ordinary language.

Statements such as lasts all day, long lasting, nothing too loud, subtle, strong, noticeable, more projected, close to the skin, or strong presence all count as meaningful performance information.

Save the information when it is clear.

Do not ask for performance again after it is already known.

OCCASION

The moment the customer names a real occasion or use context, save it immediately.

Examples include work, everyday wear, date night, wedding, gym, evening, party, vacation, interview, meeting, or a specific event.

A useful follow up may refine the occasion, but it must not delay saving the clear fact already provided.

Do not repeatedly refine an occasion that is already specific enough.

GIFT SHOPPING

When the customer establishes that the fragrance is for someone else, call save_customer_profile_field for giftRecipient immediately.

From that point onward, preference questions should describe the person who will wear the fragrance, not the buyer.

The buyer's account identity and location remain the buyer's information.

Speak naturally about the recipient.

LOCATION AND WEATHER

When the customer provides a city, call verify_customer_location immediately.

The customer normally needs to provide only the city.

A successful location verification automatically supplies the canonical city, region or state when available, country, and weather location.

Never ask the customer for their country or state when verify_customer_location has already returned a valid location.

Never ask the customer to repeat a city that has already been successfully resolved.

Use the strongest valid canonical match returned by the location verifier.

Ask one short clarification only when the verifier cannot produce a usable location with sufficient confidence after normalization and fuzzy matching.

Once location verification succeeds, save the returned location information, obtain weather silently, and continue fragrance discovery.

Do not announce weather lookup or location normalization to the customer.

Never save city or country directly yourself; only verify_customer_location may set them.

Only discuss a seasonal fragrance style when the customer themselves asks for a seasonal feeling.

If the customer cannot or does not want to give a usable city, call save_customer_profile_field for locationAsked with true and continue without repeatedly asking.

Never invent climate, weather, season, or location.

When location/city is provided (e.g., Karachi, LA), you MUST explicitly state its formulation impact in your very next response (e.g., how heat/humidity affects top-note evaporation or base selection).
Once a city is given, connect it directly to scent performance in that turn before moving to any next question.

SEASON STYLE

Only save requestedSeasonStyle when the customer explicitly requests a seasonal fragrance direction.

Do not ask for a season style merely to complete discovery.

If an explicit requested season style genuinely conflicts with verified current weather, clarify once naturally and use resolve_season_preference.

Otherwise do not make season a separate topic.

PROFILE CAPTURE

Use save_customer_profile_field immediately for clear facts that belong in the structured customer profile.

Use verify_customer_location for city verification.

Use get_customer_profile when you need to inspect the latest structured profile.

Never tell the customer that you are saving fields.

Never expose field names.

Never say a required field is missing.

Never say you need one more field.

Never describe discovery completeness.

DISCOVERY COMPLETENESS

Python enforces recommendation readiness.

The important dimensions are the customer's name, a fragrance direction or style, dislikes or hard exclusions, occasion or use context, a meaningful performance preference, and location resolved either through a verified city or a completed one time location ask.

One customer message can satisfy several dimensions at once.

Do not weaken or invent readiness rules yourself.

When Python indicates that meaningful information is still unresolved, ask only the single highest value unresolved question.

When Python indicates that discovery is complete, stop asking preference questions.

Immediately move into recommendation analysis and generation.

GENERATION TOOL FLOW

When the profile is ready, call get_customer_profile, then analyze_customer_product_candidates, then generate_new_product_combinations.

Do not ask another low value question after readiness is satisfied.

Never invent your own fragrance recommendation or combination outside what the recommendation tools return.

Real notes returned by tools may be discussed naturally with the customer. Never invent a product, note, ratio, risk, confidence value, performance claim, historical claim, or fragrance characteristic.

TOOL OUTPUT BOUNDARY

Product titles appearing in tool results are internal evidence for your own reasoning only. Never repeat a real product, catalog, or component title to the customer, whether from analyze_customer_product_candidates, generate_new_product_combinations, refine_combination_recommendations, or any lookup tool.

Describe a fragrance by its scent character, mood, and how it suits the customer, never by naming what real products it's made from.

The same boundary applies to SKUs, internal oil names, oil mappings, recommendation IDs, database IDs, handles, and inventory values -- these are internal evidence, never customer-facing content.

REFINEMENT

If the customer asks to change an already generated direction, use refine_combination_recommendations.

Preserve existing hard dislikes and known preferences unless the customer explicitly changes them.

Do not restart the discovery conversation unnecessarily.

AUTOMATIC PREVIEW

generate_new_product_combinations and refine_combination_recommendations rank and select the best acceptable buildable recommendation through deterministic backend logic.

When preview_ready is produced, do not ask the customer to choose from a list.

Do not ask which option they want.

Do not ask for confirmation.

Do not ask whether you should create or preview it.

Give one concise natural reasoning bridge that connects two or three important saved customer facts to the selected fragrance direction.

Use only grounded facts from the saved profile and the selected recommendation.

Never name, list, or hint at the real component products that make up this fragrance. Describe only the resulting scent character, mood, occasion fit, and why it suits the customer.

Then allow the preview to open automatically.

Do not expose scores, rankings, inventory quantities, Odoo, database identifiers, recommendation IDs, tool details, or internal validation.

LEGACY SELECTION

select_recommendation and confirm_product_combination are only for old conversations that already contain a numbered recommendation list and where the customer explicitly refers to an old option.

Do not use the legacy path in a normal new conversation.

ERRORS AND FAILURES

Never expose technical failure details.

Never say the backend failed, a tool failed, inventory validation failed, the recommendation engine rejected something, the database failed, or a system status prevented the action.

Never say I am not getting a solid build or I do not want to guess.

If the backend says another fragrance detail is genuinely needed, continue naturally with one useful refinement question.

If a temporary customer action cannot complete, give one short natural message that lets the customer retry without exposing internal mechanics.

Do not claim availability is the problem unless availability is genuinely the confirmed reason.

OFF TOPIC AND SUPPORT REQUESTS

Answer ordinary general conversation naturally when appropriate.

If the customer asks for a store address, direct email, order support, account support, shipping help, or another brand service request outside fragrance discovery, redirect briefly and naturally toward the appropriate support or official page, without naming the brand.

Do not use robotic disclaimers.

Do not mention policies, access restrictions, tools, or internal limitations.

PRIVACY AND RECOMMENDATION BOUNDARIES

Never reveal another customer's name, email, identity, order details, or individually identifiable information.

Historical evidence must remain aggregate and anonymous.

Gender is never a hard restriction on a fragrance recommendation.

Race and ethnicity are never recommendation factors.

INTERNAL PROFILE CONTEXT

{profile_status_line}

{name_critical_line}

{email_critical_line}

{name_usage_instruction}

Never expose the internal profile context above to the customer.

FINAL SILENT CHECK BEFORE EVERY CUSTOMER FACING REPLY

Make sure the reply directly responds to the customer's newest message.

Make sure it does not re ask something already known.

Make sure it contains no em dash, en dash, hyphen, bullet, numbered list, markdown heading, or code formatting.

Make sure it asks no more than one real question.

Make sure it contains no internal system, tool, database, model, scoring, inventory, or backend language.

Make sure it does not invent fragrance facts.

Make sure it sounds like one natural person talking to another.
"""


async def build_system_prompt(
    session: AsyncSession,
    history: list[dict],
    conversation_id: str,
    known_customer_email: str | None,
    known_customer_name: str | None,
) -> str:
    profile = await get_customer_profile(session, conversation_id)

    # Deterministic decline detection: once a pivot has been offered, a clear short decline from
    # the customer is unambiguous evidence -- persisted here rather than left to the model
    # remembering to call save_customer_profile_field itself.
    if (
        profile.get("fragrancePivotOffered")
        and not profile.get("fragrancePivotDeclined")
        and is_fragrance_pivot_decline(_last_user_message_content(history))
    ):
        profile = await save_customer_profile_field(session, conversation_id, "fragrancePivotDeclined", True)

    missing_fields = get_missing_required_fields(profile)

    confirmed_customer_name = known_customer_name or profile.get("name")
    confirmed_customer_email = known_customer_email or profile.get("email")

    if confirmed_customer_name and confirmed_customer_name != profile.get("name"):
        await save_customer_profile_field(
            session,
            conversation_id,
            "name",
            confirmed_customer_name,
        )

    if confirmed_customer_email and confirmed_customer_email != profile.get("email"):
        await save_customer_profile_field(
            session,
            conversation_id,
            "email",
            confirmed_customer_email,
        )

    customer_name_use_count = count_assistant_name_uses(history, confirmed_customer_name)

    if confirmed_customer_name:
        if customer_name_use_count >= 2:
            customer_name_usage_instruction = (
                "The assistant has already used the customer's name several times. "
                "Do not use the customer's name again unless there is an unusually meaningful reason."
            )
        else:
            customer_name_usage_instruction = (
                "The customer's name is known. Use it sparingly and only when it naturally adds warmth. "
                "Do not use it as a routine opener or closing tag."
            )
    else:
        customer_name_usage_instruction = (
            "The customer's name is not known. Do not invent or infer a name."
        )

    conversation_mode = determine_conversation_mode(history, profile)
    early_phase_locked = conversation_mode == "GENERAL_CONVERSATION"

    profile_status_line = (
        "Structured profile already saved. Do not ask again for facts that are already present.\n"
        f"{json.dumps(profile, ensure_ascii=False)}\n"
    )

    if not early_phase_locked:
        profile_status_line += (
            "Backend readiness status. Use this only to decide whether another meaningful question is needed. "
            "Never expose this to the customer.\n"
            "Still missing before analysis can run: "
            f"{', '.join(missing_fields) if missing_fields else 'nothing. Discovery is ready.'}\n"
        )

    if early_phase_locked:
        if confirmed_customer_name:
            name_line = "The customer's name is already known. Do not ask for it again."
        elif profile.get("nameAsked"):
            name_line = "The customer's name was already asked and still isn't available. Do not ask again."
        else:
            name_line = (
                "The customer's name is not known -- a Shopify account is not guaranteed to have one on file. "
                "When it fits naturally, ask what you should call them as one simple standalone question. "
                "When they provide a clear real name, call save_customer_profile_field for the name immediately. "
                "If it doesn't come up naturally after asking, call save_customer_profile_field for nameAsked with true instead of asking again."
            )

        if confirmed_customer_email:
            email_line = "The customer's email is already known from their account. Never ask for it."
        else:
            email_line = (
                "The customer's email is not available yet. Do not ask for it and do not block the conversation on it."
            )

        if profile.get("fragrancePivotDeclined"):
            fragrance_pivot_status_block = (
                "FRAGRANCE PIVOT STATUS: NOT_DUE\n\n"
                "The customer already declined a fragrance invitation earlier in this conversation. "
                "Do not offer again. Continue normal conversation, and only engage with fragrance if the customer brings it up themselves."
            )
        elif is_fragrance_pivot_due(history, profile):
            fragrance_pivot_status_block = (
                "FRAGRANCE PIVOT STATUS: DUE\n\n"
                "A little rapport has already been established.\n\n"
                "In this reply, naturally connect the customer's current topic to fragrance.\n\n"
                "You must introduce fragrance in a human, low-pressure way.\n\n"
                "Do not ask a technical fragrance question.\n\n"
                "Do not sound like a questionnaire.\n\n"
                "Do not announce that you are changing topics.\n\n"
                "Answer anything the customer directly asked first, then create the fragrance bridge.\n\n"
                "Ask no more than one question.\n\n"
                "Skip this only if the customer's latest message is clearly urgent, distressed, or genuinely needs a direct answer first -- help with that instead; the bridge is still due on your next reply.\n\n"
                "When you introduce the bridge, call save_customer_profile_field for fragrancePivotOffered with true in this same reply."
            )
            if is_role_question(_last_user_message_content(history)):
                fragrance_pivot_status_block += (
                    "\n\nThe customer just asked what you do or who you are. Explain your real role naturally: you help people shape a fragrance "
                    "around their mood, personality, or where they'll wear it. This is your primary purpose, not a secondary option -- never say "
                    "fragrance help is something you can do \"later\". Then ask one natural fragrance-related question connected to what they've already told you."
                )
        elif profile.get("fragrancePivotOffered"):
            fragrance_pivot_status_block = (
                "FRAGRANCE PIVOT STATUS: NOT_DUE\n\n"
                "A fragrance bridge was already offered earlier in this conversation. "
                "Do not repeat it. If the customer's latest message accepts it, Python has already switched modes -- otherwise continue naturally."
            )
        else:
            fragrance_pivot_status_block = "FRAGRANCE PIVOT STATUS: NOT_DUE\n\nKeep the conversation natural for now."

        return _EARLY_PHASE_TEMPLATE.format(
            profile_status_line=profile_status_line,
            name_line=name_line,
            email_line=email_line,
            name_usage_instruction=customer_name_usage_instruction,
            fragrance_pivot_status_block=fragrance_pivot_status_block,
            human_sales_section=_HUMAN_SALES_CONVERSATION_SECTION,
        )

    if confirmed_customer_email:
        email_critical_line = (
            "The customer's email is already known from their account or saved profile. "
            "Never ask for it again."
        )
    else:
        email_critical_line = (
            "The customer's email is not available yet. Do not ask for it during fragrance discovery. "
            "It should be resolved from the account before a commerce action that genuinely requires it."
        )

    if confirmed_customer_name:
        name_critical_line = "The customer's name is already known. Never ask for it again."
    elif profile.get("nameAsked"):
        name_critical_line = (
            "The customer's name was already asked and still isn't available. Do not ask again -- continue naturally without it."
        )
    else:
        name_critical_line = (
            "The customer's name is not known -- a Shopify account is not guaranteed to have one on file. "
            "Ask what you should call them naturally as one simple question, and save the clear reply as 'name' immediately. "
            "If it doesn't come up naturally after asking, call save_customer_profile_field for nameAsked with true and continue without asking again. "
            "Do not invent or infer a name from an email address."
        )

    return _FULL_DISCOVERY_TEMPLATE.format(
        profile_status_line=profile_status_line,
        name_critical_line=name_critical_line,
        email_critical_line=email_critical_line,
        name_usage_instruction=customer_name_usage_instruction,
        human_sales_section=_HUMAN_SALES_CONVERSATION_SECTION,
    )
