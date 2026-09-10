"""Port of app/services/fragranceCopyGeneration.server.js -- a small, note-aware OpenAI call for
customer-facing copy, made only for the small number of proposals actually being returned (never
for every scored candidate). Runs in parallel across the batch, with one targeted parallel retry
wave for anything that collides or breaks the "never start with This" rule, and a template
fallback for anything that still fails.

Mutates each proposal dict's customerFacingDescription/customerFacingWhySuits in place ONLY on
success -- a proposal that fails every repair step keeps whatever the caller already set (its
deterministic describeCharacter()/template fallback); this module never computes that fallback
itself.
"""

import json
import re
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.fragrance.compatibility import matched_literal_terms

_COPY_REQUEST_TIMEOUT_SECONDS = 12.0
MAX_FIELD_LENGTH = 220

# Mirrors the Node chat route's own leak-guard pattern for internal cuid-shaped IDs.
_LEAKED_ID_PATTERN = re.compile(r"\bc[a-z0-9]{20,}\b", re.IGNORECASE)


def _mentions_unearned_exact_note(text: str | None, missing_exact_notes: list[str] | None) -> bool:
    if not text or not missing_exact_notes:
        return False
    return len(matched_literal_terms([text], missing_exact_notes)) > 0


def _mentions_unmatched_family(text: str | None, missing_preference_families: list[str] | None) -> bool:
    if not text or not missing_preference_families:
        return False
    return len(matched_literal_terms([text], missing_preference_families)) > 0


OPENING_ANGLES = [
    "open by naming the blend's own standout quality as the subject of the sentence",
    'open by addressing the customer directly — start with "Your" or "You"',
    'open around the occasion/moment it\'s for, or a sensory verb like "Wearing" or "Reaching for"',
    "open with the blend's dominant note or feeling as the very first word",
]

# Quoted verbatim from the main system prompt's "Presenting results" evidenceScope rule, as a
# literal string (not a shared import) so this module has no dependency on the conversation module.
_EVIDENCE_SCOPE_RULE = (
    'never say "in your area" or "in your region" unless evidenceScope is "city", "state", or '
    '"country"; if "season_global" say something like "this direction has shown wider interest '
    'during similar seasonal conditions" (never regional); if "global" say "broader interest among '
    'customers with similar preferences" (never regional); if "limited" say historical evidence is '
    "limited and lean on compatibility/stated preferences instead (never invent a popularity claim)."
)

_BASE_RULES = f"""Rules:
- "description": one short phrase (roughly 4-10 words) describing the blend's actual character, grounded in the real notes given. Never a generic mood phrase disconnected from the actual notes.
- "whySuits": one short sentence on why this suits THIS customer, referencing their actual stated likes/preferred style/occasion where given. Never a generic catch-all sentence.
- Vary your sentence opening and structure every time. NEVER start "description" or "whySuits" with the word "This" as the literal first word.
- NEVER name a real product, SKU, product ID, internal handle, or any brand name (including this brand's own name). Describe only the blend's own character and notes.
- NEVER make any claim about regional, seasonal, or historical popularity (e.g. "popular in your area," "trending this season") — that is handled by a separate field elsewhere and phrased under its own strict rules. If you reference evidence at all, follow this exact constraint: {_EVIDENCE_SCOPE_RULE}
- Never contradict a stated dislike. Never invent a note, preference, or occasion beyond what's given below.
- The customer's stated "likes" may name a preference family (e.g. "Floral") that THIS SPECIFIC blend doesn't actually contain — "matchedFamilies" below is the subset that's actually real for this blend; "missingFamilies" is the subset that isn't. NEVER claim, imply, or reference a family listed in "missingFamilies" (e.g. never say "designed around your preference for floral scents" if "floral" is in missingFamilies) — describe what the blend actually is instead.
- Keep both fields brief and conversational — similar in length to "airy and bright" / "Designed around your preference for fresh scents." — not a paragraph."""


def _first_word_of(text: str) -> str:
    words = text.strip().split()
    first = words[0] if words else ""
    return re.sub(r"[^a-z]", "", first.lower())


def _opening_of(text: str, n: int = 4) -> str:
    return " ".join(text.strip().split()[:n]).lower()


_BRAND_NAME_PATTERN = re.compile(r"\bdua\b", re.IGNORECASE)


def _text_leaks(text: str | None, catalog_titles_lowercase: list[str]) -> bool:
    if not text:
        return True
    if _LEAKED_ID_PATTERN.search(text):
        return True
    lower = text.lower()
    if _BRAND_NAME_PATTERN.search(lower):
        return True
    return any(title in lower for title in catalog_titles_lowercase)


class CopyModelInput(BaseModel):
    """Phase 3 (F3): the complete, allowlisted input of the copy model. Note names grouped by
    the ROLE they play (never the product that carries them), the customer's own stated
    preferences, and two server-derived categorical labels. Unknown keys are rejected, so a new
    internal proposal field can never reach this model by accident."""

    model_config = ConfigDict(extra="forbid")

    notesByRole: dict[str, list[str]] = Field(default_factory=dict)
    likes: list[str] = Field(default_factory=list)
    dislikes: list[str] = Field(default_factory=list)
    preferredStyle: str | None = None
    occasion: str | None = None
    matchedFamilies: list[str] = Field(default_factory=list)
    missingFamilies: list[str] = Field(default_factory=list)
    confidence: str | None = None
    evidenceScope: str | None = None


def _payload(notes_by_role, likes, dislikes, preferred_style, occasion, matched_families, missing_families, confidence, evidence_scope) -> dict:
    return CopyModelInput(
        notesByRole={str(k): [str(n)[:60] for n in (v or [])][:20] for k, v in (notes_by_role or {}).items()},
        likes=[str(x)[:100] for x in (likes or [])][:20],
        dislikes=[str(x)[:100] for x in (dislikes or [])][:20],
        preferredStyle=(str(preferred_style)[:200] if preferred_style else None),
        occasion=(str(occasion)[:200] if occasion else None),
        matchedFamilies=[str(x)[:40] for x in (matched_families or [])][:10],
        missingFamilies=[str(x)[:40] for x in (missing_families or [])][:10],
        confidence=(str(confidence) if confidence else None),
        evidenceScope=(str(evidence_scope) if evidence_scope else None),
    ).model_dump()


def _build_prompt_initial(
    *, notes_by_role, likes, dislikes, preferred_style, occasion, matched_families, missing_families,
    confidence, evidence_scope, position, total,
) -> list[dict]:
    angle_hint = OPENING_ANGLES[position % len(OPENING_ANGLES)]
    system = (
        "You write short, appealing customer-facing copy for a single custom fragrance blend. "
        "You are given only the blend's real notes (grouped by the role each plays in the blend) "
        "and the customer's own stated preferences — nothing else about how the blend was built. "
        f"This is recommendation {position + 1} of {total} being shown to the same customer in one "
        "reply — other recommendations are being written independently, so pick a genuinely distinct "
        'angle. Respond with a JSON object with exactly two string fields: "description" and '
        '"whySuits".\n\n'
        f"{_BASE_RULES}\n- {angle_hint}"
    )
    payload = _payload(notes_by_role, likes, dislikes, preferred_style, occasion, matched_families, missing_families, confidence, evidence_scope)
    return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload)}]


def _build_prompt_retry(
    *, notes_by_role, likes, dislikes, preferred_style, occasion, matched_families, missing_families,
    confidence, evidence_scope, position, total, avoid_openings, avoid_first_words, retry_angle_index,
) -> list[dict]:
    angle_hint = OPENING_ANGLES[retry_angle_index % len(OPENING_ANGLES)]
    avoid_openings_text = ", ".join(f'"{o}"' for o in avoid_openings) if avoid_openings else "(none yet)"
    avoid_first_words_text = ", ".join(f'"{w}"' for w in avoid_first_words)
    system = (
        "You write short, appealing customer-facing copy for a single custom fragrance blend. "
        "You are given only the blend's real notes (grouped by the role each plays in the blend) "
        "and the customer's own stated preferences — nothing else about how the blend was built. "
        f"This is recommendation {position + 1} of {total} being shown to the same customer in one "
        'reply. Respond with a JSON object with exactly two string fields: "description" and '
        '"whySuits".\n\n'
        f"{_BASE_RULES}\n"
        "- RETRY — your previous attempt collided with another recommendation in this same batch "
        "(or broke a rule). You're one of several items being retried in this same repair pass — to "
        "avoid colliding with the OTHER retries too (which don't see your output either), use this "
        f"distinct angle: {angle_hint}\n"
        '- Separately, do NOT start "description" or "whySuits" with any of these already-used '
        f'phrasings from the rest of the batch: {avoid_openings_text}. Do NOT use any of these as '
        f"the first word: {avoid_first_words_text}."
    )
    payload = _payload(notes_by_role, likes, dislikes, preferred_style, occasion, matched_families, missing_families, confidence, evidence_scope)
    return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload)}]


async def _http_post(url: str, json_payload: dict, headers: dict) -> httpx.Response:
    async with httpx.AsyncClient(timeout=_COPY_REQUEST_TIMEOUT_SECONDS) as client:
        return await client.post(url, json=json_payload, headers=headers)


async def call_copy_model(messages: list[dict]) -> dict[str, str] | None:
    if not settings.openai_api_key:
        return None
    payload = {
        "model": settings.openai_copy_model,
        "messages": messages,
        "temperature": settings.openai_copy_temperature,
        "max_tokens": settings.openai_copy_max_output_tokens,
        "response_format": {"type": "json_object"},
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {settings.openai_api_key}"}
    try:
        response = await _http_post("https://api.openai.com/v1/chat/completions", payload, headers)
        # Some models (reasoning-tier ones in particular) only support the default temperature and
        # reject any explicit value with a 400 -- retry once without it, same as openai_client.py.
        for _ in range(2):
            if response.status_code != 400:
                break
            if "temperature" in payload and "temperature" in response.text and "does not support" in response.text:
                payload.pop("temperature", None)
            elif "max_tokens" in payload and "max_tokens" in response.text and "max_completion_tokens" in response.text:
                payload["max_completion_tokens"] = payload.pop("max_tokens")
            else:
                break
            response = await _http_post("https://api.openai.com/v1/chat/completions", payload, headers)
    except Exception:
        return None
    if response.status_code != 200:
        return None

    data = response.json()
    choices = data.get("choices") or []
    raw = choices[0].get("message", {}).get("content") if choices else None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None

    description = parsed.get("description")
    why_suits = parsed.get("whySuits")
    if not isinstance(description, str) or not isinstance(why_suits, str):
        return None
    if not description.strip() or not why_suits.strip():
        return None
    if len(description) > MAX_FIELD_LENGTH or len(why_suits) > MAX_FIELD_LENGTH:
        return None
    return {"description": description.strip(), "whySuits": why_suits.strip()}


async def apply_customer_facing_copy(
    items: list[dict[str, Any]], profile_fields: dict[str, Any], catalog_titles_lowercase: list[str]
) -> None:
    """items: [{"proposal": dict, "notesByRole": dict}, ...] -- proposal dicts are mutated in place."""
    import asyncio

    total = len(items)
    if not total:
        return

    # ---- Wave 1: parallel, angle-hinted ----
    async def _initial(position: int, item: dict) -> dict:
        proposal = item["proposal"]
        result = await call_copy_model(_build_prompt_initial(
            notes_by_role=item["notesByRole"],
            likes=profile_fields.get("likes"), dislikes=profile_fields.get("dislikes"),
            preferred_style=profile_fields.get("preferredStyle"), occasion=profile_fields.get("occasion"),
            matched_families=proposal.get("matchedPreferenceFamilies"),
            missing_families=proposal.get("missingPreferenceFamilies"),
            confidence=proposal.get("confidence"), evidence_scope=proposal.get("evidenceScope"),
            position=position, total=total,
        ))
        return {"proposal": proposal, "notesByRole": item["notesByRole"], "position": position, "result": result}

    initial = await asyncio.gather(*(_initial(position, item) for position, item in enumerate(items)))

    # ---- Local, cheap check: leak-check + "this" violation + within-batch collision ----
    accepted: list[dict] = []
    needs_retry: list[dict] = []
    for item in initial:
        result = item["result"]
        proposal = item["proposal"]
        if not result:
            item["reason"] = "hard-failure-no-retry"
            needs_retry.append(item)
            continue
        if _text_leaks(result["description"], catalog_titles_lowercase) or _text_leaks(result["whySuits"], catalog_titles_lowercase):
            item["reason"] = "leak-check-failed-no-retry"
            needs_retry.append(item)
            continue
        if _mentions_unearned_exact_note(result["description"], proposal.get("missingExactNotes")) or _mentions_unearned_exact_note(result["whySuits"], proposal.get("missingExactNotes")):
            item["reason"] = "exact-note-mismatch-no-retry"
            needs_retry.append(item)
            continue
        if _mentions_unmatched_family(result["description"], proposal.get("missingPreferenceFamilies")) or _mentions_unmatched_family(result["whySuits"], proposal.get("missingPreferenceFamilies")):
            item["reason"] = "family-mismatch"
            needs_retry.append(item)
            continue

        fw_desc, fw_why = _first_word_of(result["description"]), _first_word_of(result["whySuits"])
        op_desc, op_why = _opening_of(result["description"]), _opening_of(result["whySuits"])
        this_violation = fw_desc == "this" or fw_why == "this"
        collides = any(a["opDesc"] == op_desc or a["opWhy"] == op_why or a["fwDesc"] == fw_desc or a["fwWhy"] == fw_why for a in accepted)

        if this_violation or collides:
            item["reason"] = "this-violation" if this_violation else "collision"
            needs_retry.append(item)
        else:
            accepted.append({"opDesc": op_desc, "opWhy": op_why, "fwDesc": fw_desc, "fwWhy": fw_why})
            proposal["customerFacingDescription"] = result["description"]
            proposal["customerFacingWhySuits"] = result["whySuits"]

    # ---- Wave 2: ONE parallel retry pass for colliding/violating items only ----
    avoid_openings = list(dict.fromkeys(v for a in accepted for v in (a["opDesc"], a["opWhy"])))
    avoid_first_words = list(dict.fromkeys([v for a in accepted for v in (a["fwDesc"], a["fwWhy"])] + ["this"]))

    async def _retry(retry_angle_index: int, item: dict) -> None:
        if item["reason"] in ("hard-failure-no-retry", "leak-check-failed-no-retry", "exact-note-mismatch-no-retry"):
            return
        proposal = item["proposal"]
        retry_result = await call_copy_model(_build_prompt_retry(
            notes_by_role=item["notesByRole"],
            likes=profile_fields.get("likes"), dislikes=profile_fields.get("dislikes"),
            preferred_style=profile_fields.get("preferredStyle"), occasion=profile_fields.get("occasion"),
            matched_families=proposal.get("matchedPreferenceFamilies"),
            missing_families=proposal.get("missingPreferenceFamilies"),
            confidence=proposal.get("confidence"), evidence_scope=proposal.get("evidenceScope"),
            position=item["position"], total=total,
            avoid_openings=avoid_openings, avoid_first_words=avoid_first_words, retry_angle_index=retry_angle_index,
        ))
        if not retry_result:
            return
        if _text_leaks(retry_result["description"], catalog_titles_lowercase) or _text_leaks(retry_result["whySuits"], catalog_titles_lowercase):
            return
        if _mentions_unearned_exact_note(retry_result["description"], proposal.get("missingExactNotes")) or _mentions_unearned_exact_note(retry_result["whySuits"], proposal.get("missingExactNotes")):
            return
        if _mentions_unmatched_family(retry_result["description"], proposal.get("missingPreferenceFamilies")) or _mentions_unmatched_family(retry_result["whySuits"], proposal.get("missingPreferenceFamilies")):
            return

        fw_desc, fw_why = _first_word_of(retry_result["description"]), _first_word_of(retry_result["whySuits"])
        op_desc, op_why = _opening_of(retry_result["description"]), _opening_of(retry_result["whySuits"])
        still_bad = (
            fw_desc == "this" or fw_why == "this"
            or op_desc in avoid_openings or op_why in avoid_openings
            or fw_desc in avoid_first_words or fw_why in avoid_first_words
        )
        if still_bad:
            return
        proposal["customerFacingDescription"] = retry_result["description"]
        proposal["customerFacingWhySuits"] = retry_result["whySuits"]

    await asyncio.gather(*(_retry(i, item) for i, item in enumerate(needs_retry)))
    # ponytail: JS's monitoring-only cross-batch duplicate-opening/first-word logging (never
    # affects behavior) is dropped here rather than reimplemented as log noise. Re-add via the
    # app's structured logger if that monitoring signal turns out to be wanted in production.
