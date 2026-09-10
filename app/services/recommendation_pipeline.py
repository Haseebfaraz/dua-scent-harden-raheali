"""The server-side private recommendation pipeline (Phase 3, finding F3).

The conversational model no longer decides when candidates are analysed or which engine
functions run. After every turn in which the customer's structured profile changed, the
orchestrator (app/ai/conversation_flow.py) asks this module whether the profile is ready; if so
the pipeline runs the existing deterministic engine end to end -- candidate analysis, combination
generation, the auto-confirm gate, Odoo feasibility, confirmation/re-verification, persistence,
build-capability minting -- and returns ONLY:

  * a status label (READY / IDENTITY_NEEDED / NEEDS_MORE_DETAIL / TEMPORARILY_UNAVAILABLE),
  * control data for the browser (recommendation id, preview URL), which the orchestrator emits
    as an SSE event and never places in model text,
  * a CustomerSafeRecommendation for the model to explain.

Nothing here changes how recommendations are calculated: it calls the same functions in the
same order as the former model-invoked tool handlers (app/ai/tool_executor.py::run_generate /
run_refine), which remain the single implementation.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import tool_executor
from app.ai.safe_views import CustomerSafeRecommendation
from app.services.customer_profile import is_profile_ready_for_analysis


@dataclass(frozen=True)
class PipelineOutcome:
    status: str
    recommendation_id: str | None
    preview_url: str | None
    safe_recommendation: CustomerSafeRecommendation | None

    @property
    def ready(self) -> bool:
        return self.status == tool_executor.STATUS_READY

    @property
    def sse_event(self) -> dict[str, Any] | None:
        if not self.ready:
            return None
        return {"type": "preview_ready", "recommendationId": self.recommendation_id, "previewId": self.recommendation_id, "previewUrl": self.preview_url}


def _to_outcome(raw: dict[str, Any]) -> PipelineOutcome:
    return PipelineOutcome(
        status=raw["status"],
        recommendation_id=raw.get("recommendationId"),
        preview_url=raw.get("previewUrl"),
        safe_recommendation=raw.get("safeRecommendation"),
    )


def should_generate(conversation_id: str, profile: dict[str, Any], conversation_mode: str) -> bool:
    """Deterministic trigger: discovery mode, profile complete, no recommendation selected yet,
    and this exact profile state has not already been attempted (so a failed attempt is retried
    only after the customer adds something new)."""
    if conversation_mode != "FRAGRANCE_DISCOVERY":
        return False
    if profile.get("selectedRecommendationId"):
        return False
    if not is_profile_ready_for_analysis(profile):
        return False
    return not tool_executor.generation_already_attempted(conversation_id, profile)


async def run_private_recommendation(session: AsyncSession, conversation_id: str, context: dict[str, Any]) -> PipelineOutcome:
    return _to_outcome(await tool_executor.run_generate(session, conversation_id, context))


async def run_private_refinement(session: AsyncSession, conversation_id: str, context: dict[str, Any], feedback: str) -> PipelineOutcome:
    return _to_outcome(await tool_executor.run_refine(session, conversation_id, context, feedback))
