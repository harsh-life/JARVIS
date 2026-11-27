"""One deterministic evaluation of one finished task.

    TraceSource ─ build_trace (redact, bound) ─▶ RulesEvaluator
        ─▶ RulesEvaluation (frozen, bounded) ─▶ EvaluationRecord ─▶ sink

| Condition             | Outcome        |
|-----------------------|----------------|
| disabled              | nothing runs   |
| not sampled           | nothing runs   |
| evaluator raised      | `unavailable`  |
| otherwise             | `recorded`     |

The record goes to the sink and nowhere else. Nothing reads it back to make a
decision, and nothing here holds a handle that could stop a task, call a
model, resolve a secret or change a prompt or setting.
"""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from server.evaluation.ports import EvaluationRecord, EvaluationSink
from server.evaluation.rules import RulesEvaluator
from server.evaluation.trace import TraceSource, build_trace
from shared.schemas.evaluation import EvaluationKind, EvaluationOutcome

logger = logging.getLogger("hypermind.evaluation")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class EvaluationSettings:
    """The subset of `evaluation.*` the service uses."""

    sample_successful: float = 0.1
    always_on_failure: bool = True
    max_observation_chars: int = 2000


class EvaluationService:
    def __init__(
        self, *, settings: EvaluationSettings, sink: EvaluationSink,
        evaluator: RulesEvaluator | None = None,
        clock: Callable[[], datetime] = _utcnow, rng: Callable[[], float] = random.random,
    ) -> None:
        self._evaluator = evaluator or RulesEvaluator()
        self._settings = settings
        self._sink = sink
        self._clock = clock
        self._rng = rng

    @property
    def evaluator(self) -> RulesEvaluator:
        return self._evaluator

    @property
    def settings(self) -> EvaluationSettings:
        return self._settings

    def wants(self, *, failed: bool) -> bool:
        """Sampling bounds the write volume: every failed task (if configured),
        a fraction of the rest."""

        if failed and self._settings.always_on_failure:
            return True
        return self._rng() < self._settings.sample_successful

    def record_for(self, source: TraceSource) -> EvaluationRecord:
        trace = build_trace(source, kind=EvaluationKind.POST_HOC, max_chars=self._settings.max_observation_chars)
        outcome, reason, evaluation = EvaluationOutcome.RECORDED, None, None
        try:
            evaluation = self._evaluator.evaluate(trace)
        except Exception:  # noqa: BLE001 — a diagnostic never fails anything else
            logger.exception("the rules evaluator raised; recorded as unavailable")
            outcome, reason = EvaluationOutcome.UNAVAILABLE, "evaluator_error"
        if evaluation is not None and evaluation.task_id != trace.task_id:
            outcome, reason, evaluation = EvaluationOutcome.UNAVAILABLE, "wrong_task", None
        return EvaluationRecord(
            task_id=trace.task_id, user_id=trace.user_id, graph_id=trace.graph_id,
            evaluator_id=self._evaluator.id, evaluator_version=self._evaluator.version,
            kind=EvaluationKind.POST_HOC, outcome=outcome, created_at=self._clock(), reason_code=reason,
            evaluation=evaluation, redactions=trace.redactions,
        )

    async def evaluate(self, source: TraceSource) -> uuid.UUID:
        return await self._sink.record(self.record_for(source))


__all__ = ["EvaluationService", "EvaluationSettings"]
