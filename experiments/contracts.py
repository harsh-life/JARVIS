"""Run contracts: work packages, result bundles, plans (docs/31 §3).

A `ResultBundle` is a common envelope with a specialization-specific typed
body, validated against the schema named by the work package. Evidence is
referred to by stable ids, never by position. No body has a "confidence"
field: a model's self-reported confidence is not a calibrated probability, so
uncertainty is expressed as limitations and open questions instead.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from shared.schemas.intelligence import ContextPolicy

EvidenceId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,31}$")]
SubtaskId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")]
URL_PATTERN = re.compile(r"https?://[^\s)\]>\"']+")


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceItem(_M):
    id: EvidenceId
    title: str = Field(max_length=200)
    content: str = Field(max_length=20000)


class Budget(_M):
    max_cost: float = Field(ge=0)
    max_output_tokens: int = Field(ge=16, le=8192)


class WorkPackage(_M):
    task_id: str
    subtask_id: SubtaskId
    specialization: str
    instruction: str = Field(max_length=4000)
    evidence_refs: tuple[EvidenceId, ...] = ()
    context_policy: ContextPolicy
    output_schema: str
    depends_on: tuple[SubtaskId, ...] = ()
    budget: Budget
    deadline_seconds: float = Field(gt=0)


class CompletionStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"            # parsed, but some content was dropped (e.g. unknown evidence refs)
    FAILED = "failed"              # the call or the output failed
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    BUDGET_REFUSED = "budget_refused"  # refused before any call was made
    SKIPPED = "skipped"            # a dependency failed


class Usage(_M):
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    latency_ms: float = 0.0


class ErrorInfo(_M):
    code: str
    message: str = Field(max_length=300)


class ResultBundle(_M):
    task_id: str
    subtask_id: str
    specialization: str
    assignment_id: str
    config_hash: str
    status: CompletionStatus
    assumptions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    # References in the output that did not resolve to supplied evidence:
    # dropped from `evidence_refs`, kept here so scoring can count them.
    unresolved_refs: tuple[str, ...] = ()
    # URLs the model wrote. Never retrieved or validated: always unverified.
    unverified_urls: tuple[str, ...] = ()
    error: ErrorInfo | None = None
    usage: Usage = Field(default_factory=Usage)
    body: dict | None = None


# ── typed bodies ──────────────────────────────────────────────────────────

class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Finding(_Body):
    claim: str = Field(max_length=1000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)


class ResearchBody(_Body):
    answer: str = Field(max_length=4000)
    findings: list[Finding] = Field(default_factory=list, max_length=20)
    open_questions: list[str] = Field(default_factory=list, max_length=10)


class AssessmentIssue(_Body):
    description: str = Field(max_length=1000)
    severity: Literal["low", "medium", "high"] = "medium"
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)


class AssessmentBody(_Body):
    verdict: str = Field(max_length=1000)
    issues: list[AssessmentIssue] = Field(default_factory=list, max_length=20)
    contradictions: list[AssessmentIssue] = Field(default_factory=list, max_length=20)
    missing_evidence: list[str] = Field(default_factory=list, max_length=10)


class PlanStep(_Body):
    id: str = Field(max_length=32)
    description: str = Field(max_length=1000)
    depends_on: list[str] = Field(default_factory=list, max_length=10)
    # A step a human (or JARVIS's authorized pipeline) would have to take. A
    # plan proposes; it never acts.
    requires_action: bool = False


class PlanningBody(_Body):
    steps: list[PlanStep] = Field(default_factory=list, max_length=20)
    risks: list[str] = Field(default_factory=list, max_length=10)


class GenericBody(_Body):
    summary: str = Field(max_length=4000)
    points: list[str] = Field(default_factory=list, max_length=20)


BODY_MODELS: dict[str, type[_Body]] = {
    "research.v1": ResearchBody,
    "assessment.v1": AssessmentBody,
    "planning.v1": PlanningBody,
    "generic.v1": GenericBody,
}


def body_evidence_refs(body: _Body) -> list[str]:
    refs: list[str] = []
    for value in body.model_dump().values():
        items = value if isinstance(value, list) else []
        for item in items:
            if isinstance(item, dict):
                refs.extend(item.get("evidence_refs", []) or [])
    return refs


# ── plans ─────────────────────────────────────────────────────────────────

class PlanNode(_M):
    subtask_id: SubtaskId
    specialization: str
    instruction: str = Field(max_length=4000)
    evidence_refs: tuple[EvidenceId, ...] = ()
    depends_on: tuple[SubtaskId, ...] = ()


class Plan(_M):
    task_id: str
    nodes: tuple[PlanNode, ...] = Field(max_length=32)
    rationale: str | None = Field(default=None, max_length=2000)


__all__ = [
    "AssessmentBody", "BODY_MODELS", "Budget", "CompletionStatus", "ErrorInfo", "EvidenceItem", "GenericBody",
    "Plan", "PlanNode", "PlanningBody", "ResearchBody", "ResultBundle", "URL_PATTERN", "Usage", "WorkPackage",
    "body_evidence_refs",
]
