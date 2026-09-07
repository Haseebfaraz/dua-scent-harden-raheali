"""Realistic, live-model, multi-turn conversation simulation for the fragrance agent.

Runs the Zohaib persona through the actual chat orchestration (app.ai.conversation_flow.call_ai,
never mocked) against a live simulated customer and a live judge model -- see
scripts/conversation_simulation.py for the harness itself.

This makes real, paid OpenAI calls (agent + simulator + judge, times up to MAX_CUSTOMER_TURNS,
times several runs) and can take a long time. It is excluded from the normal fast test run and
only runs when explicitly requested:

    pytest -m live_ai tests/e2e/test_conversation_simulation.py -s

Only the deterministic privacy checks are hard pytest assertions (brand name / source product
title must never reach customer-facing text -- these are exactly verifiable, not a matter of
judgment). Turn-count and humanization quality are reported, not hard-failed, since a live LLM
judge's scoring is inherently noisy from run to run; flagrant problems (never reaching
preview_ready, a judge score reported as failed) are still surfaced as pytest failures.
"""

import pytest

from scripts.conversation_simulation import (
    TRANSCRIPT_DIR,
    VARIATIONS,
    _cleanup,
    print_report_line,
    resolve_judge_model,
    resolve_simulator_model,
    run_simulation,
    write_transcript_file,
)
from app.config import settings

pytestmark = pytest.mark.live_ai


@pytest.mark.parametrize("index", range(len(VARIATIONS)))
async def test_zohaib_persona_conversation_simulation(index):
    run_label = f"zohaib_{index + 1}"
    agent_model = settings.openai_model
    simulator_model = resolve_simulator_model()
    judge_model = resolve_judge_model()

    result = await run_simulation(
        run_label, VARIATIONS[index],
        agent_model=agent_model, simulator_model=simulator_model, judge_model=judge_model,
    )
    write_transcript_file(result, TRANSCRIPT_DIR)
    print_report_line(result)

    try:
        # Deterministic, exactly verifiable -- never a judgment call.
        assert result["privacy"]["brand_leak_deterministic"] is False, "assistant said the brand's own name"
        assert result["privacy"]["product_title_leak_deterministic"] is False, (
            f"assistant leaked a real component product title: {result['privacy']['leaked_titles']}"
        )

        # The whole point of this run failing to reach a recommendation is itself a real finding
        # worth surfacing loudly, not silently passing.
        assert result["previewReady"] is True, (
            f"conversation never reached preview_ready within {result['turns']} customer turns"
        )

        judge = result["judge"] or {}
        if judge.get("passed") is False:
            pytest.fail(f"judge marked this conversation as failed: {judge}")
    finally:
        await _cleanup(result["conversationId"])
