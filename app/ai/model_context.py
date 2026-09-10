"""Bounded model context (Phase 2, finding F6 / H).

Stored history and model context are different things. The database keeps every message; the
model receives only the most recent messages that fit BOTH budgets (message count and total
characters), chosen deterministically from the end of the history. Nothing is summarized and
nothing is deleted.

Tool-call integrity: an OpenAI `assistant` message carrying `tool_calls` must be followed by the
`tool` messages that answer it. Cutting the window between them would leave orphaned `tool`
messages at the start of the context, which the API rejects, so any leading `tool` messages
after the cut are dropped as well.
"""

import json
from typing import Any


def _message_size(message: dict[str, Any]) -> int:
    content = message.get("content")
    size = len(content) if isinstance(content, str) else 0
    for call in message.get("tool_calls") or []:
        try:
            size += len(json.dumps(call))
        except (TypeError, ValueError):
            size += 0
    return size


def select_model_context(history: list[dict[str, Any]], *, max_messages: int, max_chars: int) -> list[dict[str, Any]]:
    if max_messages < 1 or max_chars < 1:
        return []
    selected: list[dict[str, Any]] = []
    total = 0
    for message in reversed(history):
        size = _message_size(message)
        if selected and (len(selected) >= max_messages or total + size > max_chars):
            break
        if not selected and size > max_chars:
            # Even the newest message alone exceeds the budget: keep it (the input limit
            # upstream makes this a misconfiguration, not a customer path) and stop.
            selected.append(message)
            break
        selected.append(message)
        total += size
    selected.reverse()
    while selected and selected[0].get("role") == "tool":
        selected.pop(0)
    return selected
