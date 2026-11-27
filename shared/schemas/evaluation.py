"""The rules evaluator's output contract.

`RulesEvaluation` is what the deterministic `RulesEvaluator`
(`server/evaluation/rules.py`) records about **one** task. It is a diagnostic
record, never a control: nothing in the runtime reads it, and no field here
can carry authority — there is no capability, tier, confirmation, grant,
resume, stop, approval, prompt or configuration field, and every model is
`extra="forbid"` and frozen.

History (docs/DECISION_REGISTER.md, D-JDG-RETIRE): this module used to carry
the LLM Judge's verdict contract (`JudgeVerdict`, `ImprovementCandidate`,
`Reward`, the `stop_requested` anomaly). The Judge and its global improvement
path were retired after BR-T2 row 38 (docs/OD_A1_BR_T2.md §3e) showed one
user's private task content reaching every other user's worker prompt through
an operator-approved candidate. Those types are gone on purpose; historical
rows in the `evaluations` / `improvement_candidates` tables keep their stored
strings and are read, where they are read at all, as plain data.

Lives in `shared/schemas/` because the evaluation package, the composition
root and the dashboard all speak it, and none of them may import the others.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# 01 §1.2's `usage.kind` is locked and has no evaluation value. Historical LLM
# Judge model calls were metered as `model_call` with a `tool_id` starting with
# this prefix. Nothing writes such rows any more, but the usage policy still
# recognises the prefix so that historical rows keep the accounting they were
# written under (never charged to a user's own budget or rates).
EVALUATOR_USAGE_PREFIX = "evaluator:"

# An audit-safe identifier: what a breaker trip reason, a failure category and
# an anomaly reason must be. Never prose (the audit trail has no free text).
IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
Identifier = Annotated[str, StringConstraints(pattern=IDENTIFIER_PATTERN)]
# A reference to one step of the evaluated task's trace (`s1`, `s2`, …).
StepRef = Annotated[str, StringConstraints(pattern=r"^s[1-9][0-9]{0,4}$")]
EvaluatorId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.\-]{0,47}$")]

MAX_REDUNDANT_STEPS = 100
MAX_FAILURES = 50


class EvaluationKind(str, Enum):
    """Only post-hoc evaluation exists. (`live_window` was the retired Judge's
    in-flight mode; historical rows may still carry that string.)"""

    POST_HOC = "post_hoc"


class Anomaly(str, Enum):
    """Informational only: `suspicious` stops nothing and is read by nothing
    that decides anything. (The retired Judge's `stop_requested` value is gone:
    no evaluator can ask for a task to stop.)"""

    NONE = "none"
    SUSPICIOUS = "suspicious"


class EvaluationOutcome(str, Enum):
    """What happened to one evaluation attempt. Every outcome is recorded; none
    affects the task. (The retired Judge's `malformed` and `over_budget`
    outcomes no longer occur: the rules evaluator parses no model output and
    spends nothing.)"""

    RECORDED = "recorded"
    UNAVAILABLE = "unavailable"      # the evaluator raised; recorded, never retried


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FailureFinding(_Strict):
    step_ref: StepRef
    category: Identifier


class RulesEvaluation(_Strict):
    """One deterministic evaluation of one task. Bounded: at most
    `MAX_REDUNDANT_STEPS` step refs and `MAX_FAILURES` findings."""

    task_id: uuid.UUID
    evaluator_id: EvaluatorId
    evaluator_version: str = Field(min_length=1, max_length=32)
    kind: EvaluationKind = EvaluationKind.POST_HOC
    efficiency: float | None = Field(default=None, ge=0.0, le=1.0)
    redundant_steps: tuple[StepRef, ...] = Field(default=(), max_length=MAX_REDUNDANT_STEPS)
    failures: tuple[FailureFinding, ...] = Field(default=(), max_length=MAX_FAILURES)
    anomaly: Anomaly = Anomaly.NONE
    anomaly_reason: Identifier | None = None


__all__ = [
    "EVALUATOR_USAGE_PREFIX",
    "IDENTIFIER_PATTERN",
    "MAX_FAILURES",
    "MAX_REDUNDANT_STEPS",
    "Anomaly",
    "EvaluationKind",
    "EvaluationOutcome",
    "FailureFinding",
    "RulesEvaluation",
]
