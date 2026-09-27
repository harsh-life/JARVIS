"""What an improvement candidate may target (19 §9) — a closed registry.

    execution → evaluation → reward/credit → improvement candidate
              → HUMAN APPROVAL → versioned configuration change

A candidate is a *suggestion* for a human. This module decides only whether a
suggestion may even be queued: its target must be one of `ALLOWED_TARGETS`,
and its proposed value must be of that target's shape and within its bounds.
Everything else is rejected at creation (JDG-T9) — including every target that
would touch security policy or authorization, which is named in
`FORBIDDEN_TARGETS` so the rejection is recorded as exactly that.

Nothing here applies anything. Approval is a superuser action that lives in
the composition root (`server/composition/improvements.py`), which this
package cannot import; the Judge therefore has no path by which it could
approve its own candidate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from server.security.secret_patterns import find_secret
from shared.schemas.evaluation import ImprovementCandidate


class TargetKind(str, Enum):
    TEXT = "text"
    INTEGER = "integer"


@dataclass(frozen=True)
class TargetSpec:
    """One tunable. `subject` targets (a tool description) name what they tune
    after a colon: `worker.tool_description:files.read`."""

    kind: TargetKind
    max_chars: int = 0
    minimum: int = 0
    maximum: int = 0
    has_subject: bool = False


# 19 §9 "what a candidate may target": worker system-prompt text, tool
# descriptions shown to the worker, recovery/stall thresholds, Judge rubric
# wording, suggestion templates. The recovery ranges never exceed what the
# config schema itself accepts, and every one of them is a bound on a worker,
# never a permission (18 §9: "all values are bounds, not authority").
ALLOWED_TARGETS: dict[str, TargetSpec] = {
    "worker.system_prompt": TargetSpec(TargetKind.TEXT, max_chars=1500),
    "worker.tool_description": TargetSpec(TargetKind.TEXT, max_chars=400, has_subject=True),
    "recovery.stall_window": TargetSpec(TargetKind.INTEGER, minimum=1, maximum=10),
    "recovery.loop_repeat_limit": TargetSpec(TargetKind.INTEGER, minimum=2, maximum=10),
    "recovery.max_worker_switches": TargetSpec(TargetKind.INTEGER, minimum=0, maximum=5),
    "evaluation.rubric": TargetSpec(TargetKind.TEXT, max_chars=1500),
    "suggestion.template": TargetSpec(TargetKind.TEXT, max_chars=1000),
}

# 19 §9 "what no candidate may ever target". A candidate naming any of these
# (by prefix) is rejected as `forbidden_target`; anything else that is not in
# ALLOWED_TARGETS is rejected as `unknown_target`. The allow-list is what
# decides — this list only makes the refusal legible in the audit trail.
FORBIDDEN_TARGETS: dict[str, str] = {
    "capability": "capability_registry",
    "capabilities": "capability_registry",
    "operation": "operation_mapping",
    "operations": "operation_mapping",
    "tier": "tier_table",
    "tiers": "tier_table",
    "risk": "tier_table",
    "floor": "tier_table",
    "authorization": "authorization",
    "authz": "authorization",
    "grant": "authorization",
    "grants": "authorization",
    "confirmation": "authorization",
    "graph": "authorization",
    "role": "authorization",
    "roles": "authorization",
    "visibility": "visibility",
    "confinement": "confinement",
    "sandbox": "sandbox_egress",
    "egress": "sandbox_egress",
    "network": "sandbox_egress",
    "execution": "confinement",
    "break_glass": "break_glass",
    "secret": "secrets",
    "secrets": "secrets",
    "secretstore": "secrets",
    "breaker": "breaker",
    "judge": "judge_permissions",
    "evaluation": "judge_permissions",  # except `evaluation.rubric` (allowed above)
    "security": "security_policy",
    "superuser": "security_policy",
    "audit": "security_policy",
    "usage": "security_policy",
    "budget": "security_policy",
    "budgets": "security_policy",
}

_SUBJECT = re.compile(r"^[a-z][a-z0-9_.\-]{0,63}$")


class CandidateRejected(Exception):
    """`code` is an audit-safe identifier; the candidate's text is never in it."""

    def __init__(self, code: str, category: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.category = category


@dataclass(frozen=True)
class AcceptedCandidate:
    target: str                  # the registry key
    subject: str | None          # e.g. a tool id
    value: str | int
    evidence: tuple[str, ...]
    expected_effect: str
    proposed_change: str

    @property
    def full_target(self) -> str:
        return f"{self.target}:{self.subject}" if self.subject else self.target


def split_target(raw: str) -> tuple[str, str | None]:
    target, sep, subject = raw.strip().partition(":")
    return target, (subject if sep else None)


def forbidden_category(target: str) -> str | None:
    if target in ALLOWED_TARGETS:
        return None
    head = target.split(".", 1)[0].lower()
    return FORBIDDEN_TARGETS.get(head)


def validate_value(target: str, subject: str | None, raw: str) -> str | int:
    """The value a target would take, or `CandidateRejected`. Also used when a
    superuser approves: an approved value is re-validated, not trusted."""

    spec = ALLOWED_TARGETS.get(target)
    if spec is None:
        raise CandidateRejected("forbidden_target" if forbidden_category(target) else "unknown_target",
                                forbidden_category(target))
    if spec.has_subject != (subject is not None):
        raise CandidateRejected("bad_subject")
    if subject is not None and not _SUBJECT.fullmatch(subject):
        raise CandidateRejected("bad_subject")
    if spec.kind is TargetKind.INTEGER:
        text = raw.strip()
        if not re.fullmatch(r"[0-9]{1,3}", text):
            raise CandidateRejected("bad_value")
        number = int(text)
        if not spec.minimum <= number <= spec.maximum:
            raise CandidateRejected("value_out_of_range")
        return number
    text = raw.strip()
    if not text or len(text) > spec.max_chars:
        raise CandidateRejected("bad_value")
    # Guidance text reaches every future worker prompt: a secret-shaped value
    # is refused outright, never redacted and kept.
    if find_secret(text) is not None:
        raise CandidateRejected("secret_in_value")
    return text


def validate_candidate(candidate: ImprovementCandidate, *, task_id: str) -> AcceptedCandidate:
    """Accept a candidate for the review queue, or reject it (JDG-T9).

    Its evidence may name only the evaluated task: a Judge sees one user's one
    task (19 §4, JDG-T7), so a candidate citing any other task is not evidence
    it could have — it is rejected, not trimmed."""

    target, subject = split_target(candidate.target)
    value = validate_value(target, subject, candidate.proposed_change)
    evidence = tuple(str(e) for e in candidate.evidence) or (task_id,)
    if any(e != task_id for e in evidence):
        raise CandidateRejected("foreign_evidence")
    if find_secret(candidate.expected_effect or "") is not None:
        raise CandidateRejected("secret_in_value")
    return AcceptedCandidate(target=target, subject=subject, value=value, evidence=evidence,
                             expected_effect=candidate.expected_effect,
                             proposed_change=candidate.proposed_change.strip())


__all__ = [
    "ALLOWED_TARGETS",
    "FORBIDDEN_TARGETS",
    "AcceptedCandidate",
    "CandidateRejected",
    "TargetKind",
    "TargetSpec",
    "forbidden_category",
    "split_target",
    "validate_candidate",
    "validate_value",
]
