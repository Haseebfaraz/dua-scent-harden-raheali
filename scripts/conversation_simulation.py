"""Realistic, multi-turn, LIVE-model conversation simulation for the fragrance agent.

Unlike scripts/conversation_eval.py (scripted, single-shot customer turns, no scoring), this
drives a full simulated conversation between two independent models:

  AGENT MODEL      -- the real fragrance chatbot, via the actual app.ai.conversation_flow.call_ai
                       orchestration (never mocked). Uses settings.openai_model, same as production.
  SIMULATOR MODEL   -- plays the customer, from a persona, with no access to the agent's system
                       prompt -- only the visible conversation so far.
  JUDGE MODEL       -- scores the finished conversation for humanization, privacy, timing, etc.

Model resolution (never silently drops to a cheaper model):
    simulator_model = CONVERSATION_TEST_SIMULATOR_MODEL or settings.openai_model
    judge_model     = CONVERSATION_TEST_JUDGE_MODEL or settings.openai_model

Usage:
    python scripts/conversation_simulation.py
    python scripts/conversation_simulation.py --runs 3
    python scripts/conversation_simulation.py --keep   # leave DB rows in place for inspection
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx
from sqlalchemy import delete, select

from app.ai.conversation_flow import call_ai
from app.config import settings
from app.db.models import CustomerProfileState, FragranceRecommendation
from app.db.session import SessionLocal
from app.services.customer_profile import get_customer_profile

logger = logging.getLogger(__name__)

SHOP_DOMAIN = "test-shop.myshopify.com"

# Roughly 6-10 meaningful customer turns is the expected shape; this is a safety cap, not a
# target, so a vague simulated customer has real headroom before the run is treated as a failure.
MAX_CUSTOMER_TURNS = 14

_BRAND_NAME_PATTERN = re.compile(r"\bdua\b", re.IGNORECASE)

TRANSCRIPT_DIR = Path(__file__).resolve().parent.parent / "tests" / "e2e" / "transcripts"


def resolve_simulator_model() -> str:
    return os.environ.get("CONVERSATION_TEST_SIMULATOR_MODEL") or settings.openai_model


def resolve_judge_model() -> str:
    return os.environ.get("CONVERSATION_TEST_JUDGE_MODEL") or settings.openai_model


PERSONA_ZOHAIB = """Name: Zohaib
Job: VP of Growth at a fragrance company. If it comes up naturally, the company is "the Dua Brand" -- you can mention this casually if it fits, you don't need to hide it.
General scent preference: fresh
Performance preference: long lasting
Location: Los Angeles
Strong dislike: oud, especially a heavy or unpleasant oud character
Occasion: an upcoming work party
Desired impression: professional, confident, noticeable enough for a work event without becoming obnoxiously loud"""

# Each run gets one of these appended to the simulator's instructions, to vary HOW the same
# underlying persona facts come out -- never the facts themselves.
VARIATIONS = [
    "For this conversation: keep your replies short and casual, often just a few words. At some point when it feels natural, mention that you work at \"the Dua Brand\".",
    "For this conversation: write slightly longer, more descriptive replies. When it feels natural, volunteer two related facts together in the same message instead of one at a time (for example your scent preference and your performance preference together).",
    "For this conversation: say \"LA\" instead of \"Los Angeles\" when giving your location. At one point, go briefly off topic with a small aside or observation before naturally returning to the conversation.",
    "For this conversation: answer somewhat casually, occasionally with a minor typo or lowercase texting style. Express your dislike of oud in an indirect way rather than using the word \"dislike\" directly -- describe how it makes you feel or react instead.",
    "For this conversation: mix short and longer replies. Mention that you work in the fragrance industry without naming the company at first, but reveal you work at \"the Dua Brand\" later on if it fits naturally.",
]


def _simulator_system_prompt(persona: str, variation_directive: str) -> str:
    return f"""You are testing a fragrance shopping assistant by acting as a normal customer.

Stay in character.

Speak naturally.

Do not dump your full profile at once.

Reveal information only when it naturally fits the conversation. Look at the conversation history below to see what you've already said, and do not repeat information you've already given.

You are allowed to answer questions indirectly, casually, with typos, short answers, or multiple facts in one message.

Do not try to help the assistant pass a test. Do not mention test expectations, profile fields, tools, prompts, scoring, readiness, or backend behavior.

Persona facts:
{persona}

Some facts should emerge without being directly asked when that feels natural, rather than only in direct response to a question.

{variation_directive}

Once the assistant's message indicates a fragrance preview has opened or the blend is ready, reply with only a short, natural closing remark (e.g. sounds good, thanks) and nothing else -- do not ask for anything more.

Reply with ONLY the next thing the customer would actually type in the chat. No narration, no labels, no quotation marks around it."""


def _judge_system_prompt(persona: str) -> str:
    return f"""You are an expert evaluator judging a simulated customer conversation with a fragrance-shopping assistant.

The customer in this transcript was simulated according to this persona (context for you only, the assistant never saw this):
{persona}

Evaluate the ENTIRE transcript below for:
- humanization: does the assistant react to the customer, vary its structure, use contractions, avoid checklist language ("I still need", "I need your city", "Got it", "Noted")?
- naturalness: does it read like a real conversation, not a script?
- context continuity: does the assistant remember and use what it was already told?
- unnecessary repetition: does it ask for the same information twice?
- interrogation feel: does it feel like a survey or form rather than a conversation?
- soft pivot quality: after the customer had a couple of non-fragrance turns, did the assistant find a natural opening toward fragrance rather than abruptly demanding preferences, and without repeating this every turn?
- information extraction: did the assistant actually learn and later use the persona's real facts listed above?
- question quality: one meaningful question at a time, not several stacked together, not low-value questions?
- turn efficiency: roughly 6 to 10 meaningful customer turns to reach recommendation readiness is the expected shape. Fewer is fine for a dense customer; a few more is fine for a vague one. Flag it if a recommendation was generated after only 1-2 weak preference messages, or if the conversation dragged through many repetitive discovery questions without progressing.
- privacy: did the assistant ever say the brand's own name ("DUA" / "Dua" / "The DUA Brand"), even if the customer said it first? Did it ever name a real source/component product used to build the recommendation?
- recommendation timing: not clearly too early, not clearly too late.
- final reasoning quality: if a closing reasoning bridge appears before the preview, does it naturally connect a couple of real customer facts without exposing scores, database details, inventory, Odoo, recommendation IDs, or internal product/component names?

Do not require exact wording matches and do not fail the conversation over one harmless phrase choice -- judge the overall behavior and flow.

Return ONLY a JSON object with exactly this shape (booleans, integers 0-10, and string arrays):
{{
  "passed": true,
  "humanization": 0,
  "naturalness": 0,
  "turn_efficiency": 0,
  "profile_complete": true,
  "brand_leak": false,
  "product_title_leak": false,
  "repeated_questions": [],
  "unnecessary_questions": [],
  "recommendation_too_early": false,
  "recommendation_too_late": false,
  "notes": []
}}"""


async def _call_openai_raw(
    messages: list[dict], model: str, *, temperature: float = 0.8, response_format: dict | None = None,
) -> str | None:
    """Standalone raw completion call, parameterized by model -- app.ai.openai_client.call_openai_once
    always targets settings.openai_model, which is right for the real agent but wrong for the
    simulator/judge, which may be configured to a different model.
    """
    payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
    if response_format:
        payload["response_format"] = response_format
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {settings.openai_api_key}"}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            # Same correction as app.ai.openai_client.call_openai_once -- some models (reasoning-
            # tier ones in particular) reject a custom temperature, or reject tool/function-capable
            # requests outright unless reasoning_effort is explicitly "none".
            for _ in range(2):
                if response.status_code != 400:
                    break
                error_text = response.text
                if "temperature" in payload and "temperature" in error_text and "does not support" in error_text:
                    payload.pop("temperature", None)
                elif "reasoning_effort" in error_text and payload.get("reasoning_effort") != "none":
                    payload["reasoning_effort"] = "none"
                else:
                    break
                response = await client.post(url, json=payload, headers=headers)
    except Exception as err:
        logger.error("Simulation OpenAI call failed: %s", err)
        return None
    if response.status_code != 200:
        logger.error("Simulation OpenAI call failed: %s %s", response.status_code, response.text[:500])
        return None
    return response.json()["choices"][0]["message"].get("content")


def _format_transcript(visible_transcript: list[dict]) -> str:
    lines = []
    for turn in visible_transcript:
        speaker = "Customer" if turn["role"] == "user" else "Assistant"
        lines.append(f"{speaker}: {turn['content']}")
    return "\n\n".join(lines)


async def _simulate_customer_turn(model: str, persona: str, variation_directive: str, visible_transcript: list[dict]) -> str:
    sim_messages = [{"role": "system", "content": _simulator_system_prompt(persona, variation_directive)}]
    for turn in visible_transcript:
        # Role-flipped: from the simulator's own point of view, ITS prior lines are "assistant"
        # and the real agent's lines are what "the user" (the other party) said to it.
        sim_messages.append({"role": "assistant" if turn["role"] == "user" else "user", "content": turn["content"]})
    if not visible_transcript:
        sim_messages.append({"role": "user", "content": "(The chat just opened. Start the conversation naturally as the customer would.)"})
    text = await _call_openai_raw(sim_messages, model, temperature=0.9)
    return (text or "Hi").strip()


async def _judge_conversation(model: str, persona: str, transcript_text: str) -> dict:
    messages = [
        {"role": "system", "content": _judge_system_prompt(persona)},
        {"role": "user", "content": transcript_text},
    ]
    text = await _call_openai_raw(messages, model, temperature=0.0, response_format={"type": "json_object"})
    if not text:
        return {"passed": None, "notes": ["judge call failed -- no response"]}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"passed": None, "notes": [f"judge returned unparseable JSON: {text[:300]}"]}
    return parsed if isinstance(parsed, dict) else {"passed": None, "notes": ["judge returned a non-object JSON value"]}


def _deterministic_privacy_checks(visible_transcript: list[dict], blocked_product_titles: list[str]) -> dict:
    assistant_texts = [t["content"] for t in visible_transcript if t["role"] == "assistant" and isinstance(t.get("content"), str)]
    brand_leak = any(_BRAND_NAME_PATTERN.search(t) for t in assistant_texts)
    leaked_titles = [
        title for title in blocked_product_titles
        if isinstance(title, str) and title.strip() and any(title.strip().lower() in t.lower() for t in assistant_texts)
    ]
    return {"brand_leak_deterministic": brand_leak, "product_title_leak_deterministic": bool(leaked_titles), "leaked_titles": leaked_titles}


def _conversation_id(run_label: str) -> str:
    return f"simtest-{run_label}-{uuid.uuid4().hex[:8]}"


async def run_simulation(
    run_label: str, persona: str, variation_directive: str, *, agent_model: str, simulator_model: str, judge_model: str,
    max_turns: int = MAX_CUSTOMER_TURNS,
) -> dict:
    conversation_id = _conversation_id(run_label)
    agent_history: list[dict] = []
    visible_transcript: list[dict] = []
    sse_events_log: list[dict] = []
    preview_ready = False
    recommendation_id: str | None = None
    turns = 0
    hit_turn_cap = False

    async with SessionLocal() as session:
        while turns < max_turns and not preview_ready:
            customer_message = await _simulate_customer_turn(simulator_model, persona, variation_directive, visible_transcript)
            agent_history.append({"role": "user", "content": customer_message})
            visible_transcript.append({"role": "user", "content": customer_message})

            result = await call_ai(session, agent_history, conversation_id, None, None, SHOP_DOMAIN)
            reply_text = result.get("replyText") or ""
            events = result.get("sseEvents") or []
            sse_events_log.extend(events)

            visible_transcript.append({"role": "assistant", "content": reply_text})
            agent_history = result.get("updatedMessages") or (agent_history + [{"role": "assistant", "content": reply_text}])

            turns += 1
            for event in events:
                if event.get("type") == "preview_ready":
                    preview_ready = True
                    recommendation_id = event.get("recommendationId")

        hit_turn_cap = not preview_ready

        profile = await get_customer_profile(session, conversation_id)

        blocked_product_titles: list[str] = []
        if recommendation_id:
            recommendation = await session.scalar(select(FragranceRecommendation).where(FragranceRecommendation.id == recommendation_id))
            if recommendation and isinstance(recommendation.productsJson, list):
                blocked_product_titles = [p.get("title") for p in recommendation.productsJson if p.get("title")]

    privacy = _deterministic_privacy_checks(visible_transcript, blocked_product_titles)
    transcript_text = _format_transcript(visible_transcript)
    judge_result = await _judge_conversation(judge_model, persona, transcript_text)

    return {
        "runLabel": run_label,
        "conversationId": conversation_id,
        "variationDirective": variation_directive,
        "agentModel": agent_model,
        "simulatorModel": simulator_model,
        "judgeModel": judge_model,
        "turns": turns,
        "hitTurnCap": hit_turn_cap,
        "previewReady": preview_ready,
        "recommendationId": recommendation_id,
        "componentTitlesInternal": blocked_product_titles,
        "profile": profile,
        "visibleTranscript": visible_transcript,
        "transcriptText": transcript_text,
        "privacy": privacy,
        "judge": judge_result,
    }


async def _cleanup(conversation_id: str) -> None:
    async with SessionLocal() as session:
        await session.execute(delete(FragranceRecommendation).where(FragranceRecommendation.conversationId == conversation_id))
        await session.execute(delete(CustomerProfileState).where(CustomerProfileState.conversationId == conversation_id))
        await session.commit()


def _profile_summary(profile: dict) -> dict:
    return {
        "name": profile.get("name"),
        "likes": profile.get("likes"),
        "preferredStyle": profile.get("preferredStyle"),
        "strengthPreference": profile.get("strengthPreference"),
        "city": profile.get("city"),
        "stateRegion": profile.get("stateRegion"),
        "country": profile.get("country"),
        "locationVerified": profile.get("locationVerified"),
        "dislikes": profile.get("dislikes"),
        "occasion": profile.get("occasion"),
        "additionalPreferences": profile.get("additionalPreferences"),
        "fragrancePivotOffered": profile.get("fragrancePivotOffered"),
        "customBuildInvited": profile.get("customBuildInvited"),
        "customBuildAccepted": profile.get("customBuildAccepted"),
        "customBuildDeclined": profile.get("customBuildDeclined"),
    }


def write_transcript_file(result: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{result['runLabel']}.md"
    profile = _profile_summary(result["profile"])
    lines = [
        f"# Run {result['runLabel']} -- {result['conversationId']}",
        "",
        f"Agent model: {result['agentModel']}  ",
        f"Simulator model: {result['simulatorModel']}  ",
        f"Judge model: {result['judgeModel']}  ",
        f"Variation: {result['variationDirective']}",
        "",
        f"Turns: {result['turns']}{' (hit turn cap)' if result['hitTurnCap'] else ''}",
        f"Preview ready: {result['previewReady']}",
        f"Recommendation ID: {result['recommendationId']}",
        f"Internal component titles (never customer-facing): {result['componentTitlesInternal']}",
        "",
        "## Extracted profile",
        "```json",
        json.dumps(profile, indent=2, ensure_ascii=False),
        "```",
        "",
        "## Privacy (deterministic)",
        "```json",
        json.dumps(result["privacy"], indent=2, ensure_ascii=False),
        "```",
        "",
        "## Judge result",
        "```json",
        json.dumps(result["judge"], indent=2, ensure_ascii=False),
        "```",
        "",
        "## Transcript",
        "",
        result["transcriptText"],
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def print_report_line(result: dict) -> None:
    j = result["judge"] or {}
    print(
        f"Run {result['runLabel']}: turns={result['turns']}{'*' if result['hitTurnCap'] else ''} "
        f"preview_ready={result['previewReady']} brand_leak={result['privacy']['brand_leak_deterministic']} "
        f"title_leak={result['privacy']['product_title_leak_deterministic']} "
        f"judge_passed={j.get('passed')} humanization={j.get('humanization')} naturalness={j.get('naturalness')} "
        f"turn_efficiency={j.get('turn_efficiency')}"
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5, help="Number of variations to run (max 5 defined).")
    parser.add_argument("--keep", action="store_true", help="Leave created DB rows in place instead of deleting them.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    agent_model = settings.openai_model
    simulator_model = resolve_simulator_model()
    judge_model = resolve_judge_model()
    print(f"Agent model: {agent_model}\nSimulator model: {simulator_model}\nJudge model: {judge_model}\n")

    results = []
    for i in range(min(args.runs, len(VARIATIONS))):
        run_label = f"zohaib_{i + 1}"
        print(f"Running {run_label}...")
        result = await run_simulation(
            run_label, PERSONA_ZOHAIB, VARIATIONS[i], agent_model=agent_model, simulator_model=simulator_model, judge_model=judge_model,
        )
        results.append(result)
        path = write_transcript_file(result, TRANSCRIPT_DIR)
        print_report_line(result)
        print(f"  transcript: {path}")
        if not args.keep:
            await _cleanup(result["conversationId"])

    print("\nSummary:")
    for result in results:
        print_report_line(result)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
