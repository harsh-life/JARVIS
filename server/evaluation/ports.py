"""What the evaluation package needs from the rest of the server, as Protocols.

`server.evaluation` may not import the runtime, the audit writer, the
SecretStore, the model layer, the gateway or the composition root (pyproject:
"The rules evaluator is never an authority"). It needs exactly one thing — a
place to write its record — and the composition root satisfies it
(`server/composition/evaluation.py`).

There is deliberately no port that stops a task, reads a rubric, resolves a
secret, calls a model, queues an improvement, or changes a prompt or setting.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from shared.schemas.evaluation import EvaluationKind, EvaluationOutcome, RulesEvaluation


@dataclass(frozen=True)
class EvaluationRecord:
    """Everything one evaluation attempt produced, for the sink to persist in
    one transaction. `evaluation` is set only for `RECORDED`."""

    task_id: uuid.UUID
    user_id: uuid.UUID
    graph_id: uuid.UUID | None
    evaluator_id: str
    evaluator_version: str
    kind: EvaluationKind
    outcome: EvaluationOutcome
    created_at: datetime
    reason_code: str | None = None
    evaluation: RulesEvaluation | None = None
    redactions: tuple[str, ...] = ()


class EvaluationSink(Protocol):
    async def record(self, record: EvaluationRecord) -> uuid.UUID:
        """Persist the evaluation row and its audit rows. Returns the row's id."""
        ...


__all__ = ["EvaluationRecord", "EvaluationSink"]
