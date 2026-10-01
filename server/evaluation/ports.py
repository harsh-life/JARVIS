"""What the evaluation package needs from the rest of the server, as Protocols.

`server.evaluation` may not import the runtime, the audit writer, the
SecretStore, the gateway or the composition root (pyproject: "The Judge is
never an authority"). So it declares here, in its own vocabulary, the few
things it needs, and the composition root satisfies each one
(`server/composition/evaluation.py`).

Note what these ports do **not** offer. There is no method that grants,
authorizes, confirms, resumes, lowers a tier, activates break-glass, changes a
confinement mode, reads a secret, or approves an improvement candidate. The
one runtime-facing handle is `BreakerTripPort.trip`, whose only possible
effect is to stop a task — enforced by the deterministic breaker (18 §5.3),
and handed to this package only when the operator opted in
(`evaluation.may_request_stop`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from server.evaluation.candidates import AcceptedCandidate
from server.evaluation.metering import JudgeUsage
from shared.schemas.evaluation import Evaluation, EvaluationKind, EvaluationOutcome


class BreakerTripPort(Protocol):
    async def trip(self, *, task_id: uuid.UUID, reason: str) -> bool:
        """Ask the circuit breaker to stop one task. Returns whether a live task
        was tripped. Can only stop: a trip cannot be reversed by anyone but the
        runtime's own terminal handling (18 §5.2)."""
        ...


class EvaluationSwitches(Protocol):
    """The operator's switches, each capped by configuration: an operator can
    turn the Judge (or its stop requests) off at runtime, and back on only up
    to what the config allows. Nothing turns them on silently."""

    def judge_enabled(self) -> bool: ...

    def stop_requests_enabled(self) -> bool: ...


@dataclass(frozen=True)
class RejectedCandidate:
    code: str
    category: str | None
    target: str  # the head of the target, e.g. "capability" — an identifier, never the proposal


@dataclass(frozen=True)
class StopDecision:
    requested: bool = False
    honoured: bool = False
    reason: str | None = None
    # Why a request was not handed to the breaker (or why the breaker had no
    # live task to stop): `stop_requests_disabled`, `post_hoc`, `task_not_live`.
    not_honoured_because: str | None = None


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
    evaluation: Evaluation | None = None
    accepted_candidates: tuple[AcceptedCandidate, ...] = ()
    rejected_candidates: tuple[RejectedCandidate, ...] = ()
    usage: tuple[JudgeUsage, ...] = ()
    redactions: tuple[str, ...] = ()
    stop: StopDecision = field(default_factory=StopDecision)
    # docs/29 §18: the agent whose run this task was (owner-scoped candidates).
    agent_id: uuid.UUID | None = None


class EvaluationSink(Protocol):
    async def evaluation_spent_since(self, since: datetime) -> float:
        """Judge spend (evaluator-attributed usage) since `since`."""
        ...

    async def global_spent_since(self, since: datetime) -> float:
        """All metered spend since `since` — the global budget's ledger."""
        ...

    async def record(self, record: EvaluationRecord) -> uuid.UUID:
        """Persist the evaluation row, its accepted candidates (pending human
        review), its usage rows and its audit rows. Returns the row's id."""
        ...

    async def stop_alert(self, *, evaluator_id: str, stops_in_window: int) -> None:
        """19 §6: an evaluator stopped more tasks than the threshold."""
        ...

    async def rubric(self) -> str | None:
        """The currently approved `evaluation.rubric` text, if any."""
        ...


__all__ = [
    "BreakerTripPort",
    "EvaluationRecord",
    "EvaluationSink",
    "EvaluationSwitches",
    "RejectedCandidate",
    "StopDecision",
]
