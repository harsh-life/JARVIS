"""Every Judge model call is metered — by construction (19 §8, USAGE-001).

The Judge's model is wrapped in `MeteredJudgeModel` at the composition root.
The wrapper refuses to call the model unless a `JudgeMeter` is bound for the
evaluation in progress (`CURRENT_JUDGE_METER`), prechecks the call's projected
cost against the **evaluation** budget before it is made, and records the
actual usage after it — success, failure and timeout alike. There is no other
handle on the Judge's model, so there is no unmetered path.

The meter belongs to one evaluation of one task. It never touches the
evaluated task's budget, and its records are attributed to the evaluator
(`shared.schemas.evaluation.EVALUATOR_USAGE_PREFIX`), not charged to the user's
own daily budget or rate limits (19 §8). Whether Judge spend also counts
toward the global daily budget is OD-JDG-2 (`evaluation.budget_scope`).
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Protocol, Sequence

from server.models.provider import ChatMessage, ModelProvider, ModelResult


class JudgeOverBudget(Exception):
    """The evaluation budget (or, with `own_and_global`, the global budget)
    would be exceeded. Raised *before* any call; nothing is charged."""

    def __init__(self, limit: str) -> None:
        super().__init__(limit)
        self.limit = limit


class UnmeteredJudgeCall(Exception):
    """A Judge model call was attempted with no meter bound. Refused."""


@dataclass(frozen=True)
class JudgeUsage:
    provider: str
    model: str
    units: int
    cost: float


class JudgeMeter(Protocol):
    async def precheck(self, projected_cost: float) -> None:
        """Raise `JudgeOverBudget` if the call may not be made."""
        ...

    def record(self, usage: JudgeUsage) -> None:
        """Keep one call's usage. Synchronous and in memory, so recording can
        never be skipped by a cancellation; the evaluation service writes the
        records to the ledger afterwards."""
        ...


CURRENT_JUDGE_METER: contextvars.ContextVar[JudgeMeter | None] = contextvars.ContextVar(
    "hypermind_judge_meter", default=None
)


@dataclass
class RecordingMeter:
    """The meter one evaluation uses: a precheck callback plus the records."""

    budget_check: "BudgetCheck"
    records: list[JudgeUsage] = field(default_factory=list)

    async def precheck(self, projected_cost: float) -> None:
        await self.budget_check(projected_cost + sum(r.cost for r in self.records))

    def record(self, usage: JudgeUsage) -> None:
        self.records.append(usage)


class BudgetCheck(Protocol):
    async def __call__(self, projected_cost: float) -> None: ...


class MeteredJudgeModel:
    """A `ModelProvider` that cannot be called unmetered."""

    def __init__(self, inner: ModelProvider) -> None:
        self._inner = inner
        self.spec = inner.spec

    async def invoke(self, messages: Sequence[ChatMessage], *, timeout: float) -> ModelResult:
        meter = CURRENT_JUDGE_METER.get()
        if meter is None:
            raise UnmeteredJudgeCall("a Judge model call needs a bound meter")
        spec = self._inner.spec
        await meter.precheck(spec.projected_cost(prompt_chars=sum(len(m.content) for m in messages)))
        try:
            result = await self._inner.invoke(messages, timeout=timeout)
        except BaseException:
            # A failed or abandoned call is still a call (the runtime meters
            # its own failures the same way).
            meter.record(JudgeUsage(spec.provider, spec.model, 0, 0.0))
            raise
        cost = spec.pricing.cost(prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens)
        meter.record(JudgeUsage(spec.provider, spec.model, result.total_tokens, cost))
        return result

    async def health(self) -> bool:
        return await self._inner.health()


__all__ = [
    "CURRENT_JUDGE_METER",
    "BudgetCheck",
    "JudgeMeter",
    "JudgeOverBudget",
    "JudgeUsage",
    "MeteredJudgeModel",
    "RecordingMeter",
    "UnmeteredJudgeCall",
]
