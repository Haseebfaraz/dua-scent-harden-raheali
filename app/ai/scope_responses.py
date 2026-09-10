"""Server-authored, customer-safe replies for the non-fragrance routes (Phase 4).

These are deterministic: no model call, no tools, no private data. They never mention
classification, rules, attacks, tools, prompts, or security. They sound like a specialised
fragrance designer staying in its lane, and they always hand the conversation back to fragrance.
Variation is picked deterministically from the conversation id and turn count so a customer
does not see the same sentence twice in a row.
"""

import hashlib

_OFF_TOPIC = (
    "I'm your fragrance designer, so that one's outside my lane. If you'd like, tell me the kind of scent you're trying to create and I'll take it from there.",
    "That's not something I can help with here, but fragrance is. What mood or moment are you designing a scent for?",
    "I'll leave that to someone else and stay on scent. Is there a fragrance you've loved, or a feeling you'd want yours to carry?",
    "Not my area, but I'd love to design something for you. What do you want your fragrance to feel like when you wear it?",
)

_OFF_TOPIC_CODE = (
    "I'm your fragrance designer, so coding is outside my lane. If you want, tell me the kind of scent you're trying to create.",
    "Code isn't something I do here, but scent is. Where would you wear the fragrance we make together?",
)

_ATTACK = (
    "I can walk you through how the fragrance experience works at a high level, but the details of how I'm set up stay behind the scenes. Tell me what kind of scent you'd like to create and we'll get going.",
    "How I work under the hood isn't something I share, but designing your fragrance is exactly what I'm here for. What would you like it to feel like?",
    "Let's keep this about your scent. Tell me a note you love, or a moment you're designing for, and I'll shape a direction around it.",
)

_SERVICE_META = {
    "what_do_you_do": "I help you design a personal fragrance. You tell me what you love, what you can't stand, where and when you'd wear it, and I shape a scent profile around that, then match it to the blending system behind this experience. Want to start with a note or a mood you're drawn to?",
    "how_it_works": "It's a conversation. As you tell me about the notes, feelings, occasions and strength you want, I build a profile of your taste. When there's enough to go on, the blending system behind this experience matches that profile to a blend, and you get to preview it, rename it, and fine tune the balance before it's made. What's the first thing you'd want your scent to say?",
    "is_custom": "Yes, the scent is shaped around your own preferences and blended to order, not pulled off a shelf. Tell me a note or a feeling you want at the heart of it and I'll start there.",
    "are_you_ai": "You're talking with the studio's AI fragrance designer, and I'm here for one thing: helping you create a scent that feels like yours. Where would you wear it most?",
    "after_design": "Once we've designed it, you'll see a preview where you can rename it and adjust the balance of the top, middle and base, then save it or add it to your cart. If you change your mind later, you can come back and refine the scent. Shall we keep going?",
    "can_change": "Absolutely. You can adjust the balance and the name on the preview, and you can ask me to make the scent fresher, sweeter, softer, or whatever direction you want. What would you like to change?",
    "generic": "I'm your personal fragrance designer: I learn your taste and shape a scent around it. What kind of fragrance are you in the mood to create?",
}


def _pick(options: tuple[str, ...], seed: str) -> str:
    index = int(hashlib.sha256(seed.encode()).hexdigest(), 16) % len(options)
    return options[index]


def off_topic_reply(reason_code: str, seed: str) -> str:
    if reason_code == "OFF_TOPIC_CODE":
        return _pick(_OFF_TOPIC_CODE, seed)
    return _pick(_OFF_TOPIC, seed)


def attack_reply(seed: str) -> str:
    return _pick(_ATTACK, seed)


def service_meta_reply(message: str) -> str:
    text = (message or "").lower()
    if "after" in text or "next" in text or "when we" in text or "finished" in text or "done" in text:
        return _SERVICE_META["after_design"]
    if "change" in text or "edit" in text or "adjust" in text or "modify" in text or "rename" in text:
        return _SERVICE_META["can_change"]
    if "custom" in text or "bespoke" in text or "unique" in text or "personali" in text or "made for me" in text:
        return _SERVICE_META["is_custom"]
    if " ai" in f" {text}" or "bot" in text or "robot" in text or "human" in text or "real person" in text or "chatgpt" in text:
        return _SERVICE_META["are_you_ai"]
    if "how" in text and ("work" in text or "choose" in text or "pick" in text or "decide" in text or "select" in text or "match" in text or "made" in text or "built" in text):
        return _SERVICE_META["how_it_works"]
    if "what do you do" in text or "who are you" in text or "what are you" in text or "your task" in text or "your job" in text or "your role" in text or "your purpose" in text or "what can you" in text:
        return _SERVICE_META["what_do_you_do"]
    return _SERVICE_META["generic"]


def scope_redirect_reply(seed: str) -> str:
    """Deterministic replacement when generated output failed scope/leak validation."""
    return _pick(_ATTACK, seed)
