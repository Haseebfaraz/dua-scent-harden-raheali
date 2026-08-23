"""Port of app/services/legacyPreviewRecovery.server.js -- a conversation that predates the
auto-preview flow can already have a numbered list of combinations in its history, with the
customer typing "1", "preview", "yes", etc. expecting a redirect. Resolved deterministically,
entirely bypassing the model, before the conversation loop ever runs.

Returns None (falls through to the normal conversational flow) when the message doesn't look like
a legacy selection/confirmation at all, or nothing can be resolved for it.
"""

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.preview_url import build_preview_url
from app.db.models import FragranceRecommendation
from app.services.customer_profile import get_customer_profile, save_customer_profile_fields
from app.services.recommendation_confirmation import confirm_recommendation

_LEGACY_SELECTION_PATTERN = re.compile(
    r"^\s*(\d{1,2}|first|second|third|last|preview|yes|confirm|create it|create this|create that one)\s*[.!]?\s*$",
    re.IGNORECASE,
)


async def resolve_legacy_preview_short_circuit(
    session: AsyncSession, conversation_id: str, user_message: str | None, customer_name: str | None,
    customer_email: str | None, shop_domain: str,
) -> dict[str, Any] | None:
    if not _LEGACY_SELECTION_PATTERN.match(user_message or ""):
        return None

    profile = await get_customer_profile(session, conversation_id)
    recommendation_id = profile.get("selectedRecommendationId")

    if not recommendation_id:
        digit_match = re.search(r"\d{1,2}", user_message)
        candidates = list(
            (
                await session.execute(
                    select(FragranceRecommendation)
                    .where(
                        FragranceRecommendation.conversationId == conversation_id,
                        FragranceRecommendation.status.in_(["pending", "confirmed"]),
                    )
                    .order_by(FragranceRecommendation.createdAt.desc())
                    .limit(10)
                )
            ).scalars()
        )
        rank_ordered = list(reversed(candidates))  # oldest-of-the-batch first == rank order
        if not rank_ordered:
            return None

        if digit_match:
            idx = int(digit_match.group(0)) - 1
            recommendation_id = rank_ordered[idx].id if 0 <= idx < len(rank_ordered) else None
        elif re.match(r"^\s*first\s*[.!]?\s*$", user_message, re.IGNORECASE):
            recommendation_id = rank_ordered[0].id
        elif re.match(r"^\s*second\s*[.!]?\s*$", user_message, re.IGNORECASE):
            recommendation_id = rank_ordered[1].id if len(rank_ordered) > 1 else None
        elif re.match(r"^\s*third\s*[.!]?\s*$", user_message, re.IGNORECASE):
            recommendation_id = rank_ordered[2].id if len(rank_ordered) > 2 else None
        elif re.match(r"^\s*last\s*[.!]?\s*$", user_message, re.IGNORECASE):
            recommendation_id = rank_ordered[-1].id
        else:
            # Bare "preview"/"yes"/"confirm"/"create it" with no explicit number and nothing
            # already selected -- the most recently generated recommendation is the only
            # reasonable target.
            recommendation_id = candidates[0].id

    if not recommendation_id:
        return None

    result = await confirm_recommendation(
        session, recommendation_id=recommendation_id, customer_name=customer_name, customer_email=customer_email
    )
    # "already been confirmed" is a SUCCESS state here -- the customer is very likely re-sending
    # "preview" because the redirect never fired the first time.
    if not result["ok"] and "already been confirmed" not in (result.get("reason") or ""):
        return None

    await save_customer_profile_fields(session, conversation_id, {"selectedRecommendationId": recommendation_id})
    return {"recommendationId": recommendation_id, "previewUrl": build_preview_url(shop_domain, recommendation_id)}
