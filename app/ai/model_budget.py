"""Per-turn budget for outbound model requests (Phase 9, B15 / F6).

Every HTTP request this process sends to the model provider, whatever its purpose (scope
classifier, extraction, main completion, bridge, repair, copy generation, including the bounded
parameter-correction resends after a 400), passes through `try_start()` immediately before the
request is sent. The budget is a plain counter held in a context variable: the route creates it
once per customer turn, and every coroutine and task spawned inside that turn (including the
parallel copy-generation waves) shares the same object. When it is exhausted, no further request
starts; the caller sees the same failure it already handles for an unreachable provider (the
classifier resolves UNRESOLVED, the conversation loop answers with its outage text, copy
generation keeps the deterministic template text). Nothing the model says can raise the limit.

Code that runs outside a turn (operator scripts, tests) gets an implicit budget of the same size
for the current context; `begin_turn_budget()` replaces it.
"""

import contextvars
import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class TurnModelBudget:
    limit: int
    started: int = 0
    refused: int = 0
    started_by_kind: dict[str, int] = field(default_factory=dict)

    @property
    def remaining(self) -> int:
        return max(self.limit - self.started, 0)

    def try_start(self, kind: str) -> bool:
        if self.started >= self.limit:
            self.refused += 1
            if self.refused == 1:
                logger.warning("MODEL_BUDGET_EXHAUSTED %s", json.dumps({"limit": self.limit, "startedByKind": self.started_by_kind, "refusedKind": kind}))
            return False
        self.started += 1
        self.started_by_kind[kind] = self.started_by_kind.get(kind, 0) + 1
        return True


_budget: contextvars.ContextVar[TurnModelBudget | None] = contextvars.ContextVar("model_budget", default=None)


def _default_limit() -> int:
    from app.config import settings

    return int(settings.chat_max_model_requests_per_turn)


def begin_turn_budget(limit: int | None = None) -> TurnModelBudget:
    """Start a fresh budget for the current context (one customer turn)."""
    budget = TurnModelBudget(limit=_default_limit() if limit is None else int(limit))
    _budget.set(budget)
    return budget


def current_budget() -> TurnModelBudget:
    budget = _budget.get()
    if budget is None:
        budget = begin_turn_budget()
    return budget


def try_start(kind: str) -> bool:
    """Called at the outbound boundary right before a model request is sent."""
    return current_budget().try_start(kind)
