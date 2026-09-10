"""Reusable model-boundary test helper (Phase 3).

`capture_model_requests(monkeypatch)` intercepts EVERY outbound model call (the conversational /
extraction / bridge / repair completions in app.ai.conversation_flow, and the copy model in
app.services.copy_generation) and records the exact payload each would have sent: messages, tools,
tool_choice. `assert_model_context_customer_safe(captured, canaries)` then proves two things:

  1. CANARY: none of the private sentinel strings appear anywhere in any request (messages,
     tool schemas, or tool results), serialized as JSON.
  2. STRUCTURE: none of the forbidden private field names appear as keys inside any JSON-shaped
     tool result / data message the model received.

Use it from any test that drives a chat turn with mocked private data.
"""

import json
import re
from typing import Any

from app.ai import conversation_flow
from app.services import copy_generation

# Private sentinel values. They only ever exist inside mocked internal results; if any of them
# reaches a model request the boundary is broken.
CANARIES = {
    "source_title": "ULTRA_SECRET_SOURCE_FRAGRANCE_9F2C",
    "source_title_2": "ULTRA_SECRET_SOURCE_FRAGRANCE_7B1D",
    "handle": "ultra-secret-handle-9f2c",
    "sku": "SECRET-SKU-88421",
    "score": "987654.321",
    "cohort": "PRIVATE_COHORT_CANARY",
    "odoo": "ODOO_PRIVATE_CANARY",
    "shopify": "SHOPIFY_PRIVATE_CANARY",
    "collection": "PRIVATE_COLLECTION_CANARY",
    "inspiration": "PRIVATE_INSPIRATION_CANARY",
    "recommendation_id": "recid-private-canary-0001",
    "profile_control": "PRIVATE_PROFILE_CONTROL_CANARY",
}

# Field names that only exist on private objects. Their presence as a JSON key in model context
# means an internal object was serialized wholesale.
FORBIDDEN_MODEL_KEYS = {
    "productName", "normalizedProductName", "productTitle", "internalProducts", "productsJson", "componentProductsJson",
    "handle", "collection", "inspirationName", "inspirationBrand",
    "relevanceScore", "finalScore", "preferenceScore", "historyScore", "compatibilityScore", "customerFitScore",
    "confidenceBreakdown", "riskBreakdown", "riskPenalty", "scoreJson", "evidenceJson", "historicalEvidence",
    "sameCityOrders", "sameStateOrders", "sameCountryOrders", "sameSeasonOrders", "distinctSimilarCustomers", "repeatPurchaseCustomers",
    "orderHistoryNotes", "evidenceLevel", "evidenceScope", "candidateProducts",
    "odooSku", "odooProductId", "onHandQty", "availableOilMl", "mappingStatus", "limitingSku", "maxBuildableBottles", "skusQueried", "inventoryValidated",
    "shopifyProductId", "shopifyVariantId",
    "recommendationId", "selectedRecommendationId", "pendingRecreateRecommendationId", "conversationId",
    "tokenHash", "buildToken", "conversationToken", "email",
    "autoConfirmEligible", "autoConfirmReasons", "canonicalKey", "components",
}

_KEY_PATTERN = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:')


def capture_model_requests(monkeypatch, *, reply_text: str = "Here is what I put together for you.", tool_calls_for_turn=None):
    """Patch every model client. Returns the list of captured request dicts:
    {"kind": "chat"|"copy", "messages": [...], "tools": [...], "tool_choice": ...}."""
    captured: list[dict[str, Any]] = []
    tool_calls_for_turn = tool_calls_for_turn or {}
    turn_counter = {"n": 0}

    async def _chat(messages, tools, tool_choice=None):
        captured.append({"kind": "chat", "messages": json.loads(json.dumps(messages)), "tools": json.loads(json.dumps(tools)) if tools else None, "tool_choice": tool_choice})
        if tool_choice:
            return {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}
        turn_counter["n"] += 1
        wanted = tool_calls_for_turn.get(turn_counter["n"])
        if wanted:
            return {"choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": wanted}}]}
        return {"choices": [{"finish_reason": "stop", "message": {"content": reply_text}}]}

    async def _copy(messages):
        captured.append({"kind": "copy", "messages": json.loads(json.dumps(messages)), "tools": None, "tool_choice": None})
        return {"description": "bright and airy", "whySuits": "Built around your love of fresh scents."}

    monkeypatch.setattr(conversation_flow, "call_openai_once", _chat)
    monkeypatch.setattr(copy_generation, "call_copy_model", _copy)
    return captured


def serialized(request: dict[str, Any]) -> str:
    return json.dumps(request, ensure_ascii=False)


def customer_facing_requests(captured: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return list(captured)


def assert_model_context_customer_safe(captured: list[dict[str, Any]], canaries: dict[str, str] | None = None, *, forbidden_keys: set[str] | None = None) -> None:
    canaries = canaries or CANARIES
    forbidden_keys = forbidden_keys if forbidden_keys is not None else FORBIDDEN_MODEL_KEYS
    assert captured, "no model request was captured -- the test did not exercise a model path"
    for index, request in enumerate(captured):
        blob = serialized(request)
        for label, canary in canaries.items():
            assert canary not in blob, f"private canary {label!r} reached model request #{index} ({request['kind']})"
        for message in request["messages"]:
            content = message.get("content")
            if not isinstance(content, str):
                continue
            keys = set(_KEY_PATTERN.findall(content))
            leaked = keys & forbidden_keys
            assert not leaked, f"forbidden private keys {sorted(leaked)} in a {message.get('role')} message of model request #{index}"
        for tool in request.get("tools") or []:
            name = tool["function"]["name"]
            assert name in {"save_customer_profile_field", "verify_customer_location", "resolve_season_preference", "refine_fragrance_recommendation", "record_profile_updates"}, f"private tool advertised to the model: {name}"
