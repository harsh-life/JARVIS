"""The Judge's output contract (19 §5, §9).

`Evaluation` is what an `EvaluationProvider` produces about **one** task. It is
a record, never a control: nothing in the runtime reads `quality`,
`efficiency` or `reward` to make a decision (19 §5), and no field here can
carry authority — there is no capability, tier, confirmation, grant, resume or
approval field, and every model is `extra="forbid"`, so a Judge answer that
tries to add one ("approve": true, "resume": …, "authorize": …) is malformed
and rejected, never coerced (19 §5, JDG-T4).

`JudgeVerdict` is the exact JSON object an LLM judge must emit. It is narrower
than `Evaluation`: the task id, the evaluator's identity and the evaluation
kind are filled in by deterministic code, so a Judge cannot attribute its
output to another task or another evaluator.

`ImprovementCandidate.target` is validated against the closed registry in
`server/evaluation/candidates.py` (19 §9): a candidate aimed at anything else —
the capability registry, the tier table, authorization, confinement, the
SecretStore, the breaker, or the Judge's own permissions — is rejected at
creation.

Lives in `shared/schemas/` because the evaluation package, the composition
root, the gateway and the dashboard all speak it, and none of them may import
the others.
"""

from __future__ import annotations

import math
import uuid
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

# 01 §1.2's `usage.kind` is locked and has no evaluation value (OD-JDG-4 is
# open). A Judge model call is therefore metered as `model_call`, attributed to
# the evaluator through the usage row's `tool_id`, which starts with this
# prefix. The usage policy keeps such rows out of every per-user and per-device
# limit (19 §8: evaluating a task never exhausts that user's budget).
EVALUATOR_USAGE_PREFIX = "evaluator:"

# An audit-safe identifier: what a breaker trip reason, a failure category and
# an anomaly reason must be. Never prose (the audit trail has no free text).
IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
Identifier = Annotated[str, StringConstraints(pattern=IDENTIFIER_PATTERN)]
# A reference to one step of the evaluated task's trace (`s1`, `s2`, …).
StepRef = Annotated[str, StringConstraints(pattern=r"^s[1-9][0-9]{0,4}$")]
EvaluatorId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.\-]{0,47}$")]

MAX_NOTE_CHARS = 500
MAX_PROPOSED_CHANGE_CHARS = 2000
MAX_EXPECTED_EFFECT_CHARS = 500
MAX_CANDIDATES = 5


class EvaluationKind(str, Enum):
    POST_HOC = "post_hoc"
    LIVE_WINDOW = "live_window"


class Anomaly(str, Enum):
    """`suspicious` stops nothing. `stop_requested` is a *request* to the
    deterministic breaker, honoured only when the operator opted in
    (`evaluation.may_request_stop`) — the breaker enforces, never the Judge."""

    NONE = "none"
    SUSPICIOUS = "suspicious"
    STOP_REQUESTED = "stop_requested"


class EvaluationOutcome(str, Enum):
    """What happened to one evaluation attempt (19 §7). Every outcome is
    recorded; none of them affects the task."""

    RECORDED = "recorded"
    UNAVAILABLE = "unavailable"      # provider down or timed out
    MALFORMED = "malformed"          # output rejected, never coerced
    OVER_BUDGET = "over_budget"      # skipped before any call; nothing charged


class CandidateStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


def _finite(value: float | None) -> float | None:
    if value is not None and not math.isfinite(value):
        raise ValueError("must be a finite number")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FailureFinding(_Strict):
    step_ref: StepRef
    category: Identifier
    note: str = Field(default="", max_length=MAX_NOTE_CHARS)


class Reward(_Strict):
    """Credit assignment, stored and never consumed: there is no training loop
    in this build (PRD §21, 19 §9)."""

    credit: float = Field(ge=-1.0, le=1.0)
    attributed_to: list[StepRef] = Field(default_factory=list, max_length=50)

    _finite_credit = field_validator("credit")(_finite)


class ImprovementCandidate(_Strict):
    """19 §9. `target` is a string here so that a forbidden target can be named
    in the rejection's audit row; `server/evaluation/candidates.py` decides."""

    target: str = Field(min_length=1, max_length=96)
    proposed_change: str = Field(min_length=1, max_length=MAX_PROPOSED_CHANGE_CHARS)
    evidence: list[uuid.UUID] = Field(default_factory=list, max_length=10)
    expected_effect: str = Field(default="", max_length=MAX_EXPECTED_EFFECT_CHARS)


class JudgeVerdict(_Strict):
    """The one JSON object an LLM judge answers with. Anything else — prose,
    two objects, an extra key, a score out of range, a reference to a step the
    trace does not have — is malformed."""

    quality: float | None = Field(default=None, ge=0.0, le=1.0)
    efficiency: float | None = Field(default=None, ge=0.0, le=1.0)
    redundant_steps: list[StepRef] = Field(default_factory=list, max_length=100)
    failures: list[FailureFinding] = Field(default_factory=list, max_length=50)
    anomaly: Anomaly = Anomaly.NONE
    anomaly_reason: Identifier | None = None
    reward: Reward | None = None
    improvement_candidates: list[ImprovementCandidate] = Field(default_factory=list, max_length=MAX_CANDIDATES)

    _finite_scores = field_validator("quality", "efficiency")(_finite)

    @model_validator(mode="after")
    def _reason_iff_anomaly(self) -> "JudgeVerdict":
        if self.anomaly is not Anomaly.NONE and not self.anomaly_reason:
            raise ValueError("an anomaly needs an anomaly_reason identifier")
        if self.anomaly is Anomaly.NONE and self.anomaly_reason:
            raise ValueError("anomaly_reason without an anomaly")
        return self

    def step_refs(self) -> set[str]:
        refs = set(self.redundant_steps) | {f.step_ref for f in self.failures}
        if self.reward is not None:
            refs |= set(self.reward.attributed_to)
        return refs


class Evaluation(JudgeVerdict):
    """19 §5 — one task, one evaluator, one kind."""

    task_id: uuid.UUID
    evaluator_id: EvaluatorId
    evaluator_version: str = Field(min_length=1, max_length=32)
    kind: EvaluationKind


__all__ = [
    "EVALUATOR_USAGE_PREFIX",
    "IDENTIFIER_PATTERN",
    "Anomaly",
    "CandidateStatus",
    "Evaluation",
    "EvaluationKind",
    "EvaluationOutcome",
    "FailureFinding",
    "ImprovementCandidate",
    "JudgeVerdict",
    "Reward",
]
