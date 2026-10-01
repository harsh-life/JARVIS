"""The TaskTrace — what a Judge is allowed to see about one task (19 §4).

Assembled from records that already exist, in the runtime's own vocabulary:

* the task record — the user's request (verbatim), mode, final status, failure
  code, counters, worker switches;
* the volatile transcript — the worker's own proposals and the observations
  the runtime fed back (never the system prompt, never the hydrated memory or
  vault context: those are not artifacts the worker produced);
* the task's event stream — the same events the runtime writes to the audit
  trail (authorization decisions with their tier, confirmations, tool calls
  and their outcomes, usage, worker switches, stalls, breaker trips), mirrored
  in memory so that a live window can be evaluated while the task's own
  request still holds its uncommitted audit rows.

Four rules, each enforced here rather than hoped for downstream:

* **One task, one user.** A `TaskTrace` is built from exactly one
  `TraceSource`, which carries exactly one `user_id`. There is no type that
  holds two, and `build_trace` takes one (JDG-T7).
* **No secret value.** Agents hold handles, and resolved values never enter an
  observation (12, SS-T1). On top of that every free-text field is passed
  through `redact_secrets` — the repository's secret patterns — before the
  trace exists; each hit is returned by *name* so the caller can audit it
  without the value (JDG-T6).
* **Bounded.** Each text is cut to `max_chars`; the step list is capped; a live
  window is the last `window_steps` steps.
* **Only produced artifacts.** The Judge sees what the worker output and what
  the runtime recorded — never hidden model internals, which nothing here
  claims to have.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from server.security.secret_patterns import redact_secrets
from shared.schemas.evaluation import EvaluationKind

MAX_STEPS = 400


@dataclass(frozen=True)
class SourceEvent:
    """One runtime fact. `position` is where in the transcript it happened (the
    number of transcript messages before it), so events and messages can be
    interleaved in order."""

    position: int
    at: float
    name: str
    result: str | None = None
    resource: str | None = None
    decision: str | None = None
    risk: str | None = None
    units: int | None = None
    cost: float | None = None


@dataclass(frozen=True)
class SourceMessage:
    role: str  # "assistant" (a proposal) | "user" (an observation fed back)
    content: str


@dataclass(frozen=True)
class TraceSource:
    """Everything a trace may be built from, for one task of one user. Built by
    the composition root from the runtime's snapshot; the evaluation package
    never sees the runtime's own objects."""

    task_id: uuid.UUID
    user_id: uuid.UUID
    graph_id: uuid.UUID | None
    mode: str
    user_request: str
    status: str
    failure_code: str | None
    final_response: str | None
    unresolved: bool
    iterations: int
    model_calls: int
    tool_calls: int
    worker_switches: int
    denials: int
    violations: int
    rejections: int
    tripped_source: str | None
    elapsed_seconds: float
    messages: tuple[SourceMessage, ...] = ()
    events: tuple[SourceEvent, ...] = ()
    # docs/29 §18: set when the task is a run of a user's agent.
    agent_id: uuid.UUID | None = None
    agent_run_id: uuid.UUID | None = None
    agent_version: int | None = None


@dataclass(frozen=True)
class TraceStep:
    ref: str
    kind: str  # "proposal" | "observation" | "event"
    text: str = ""
    name: str | None = None
    result: str | None = None
    resource: str | None = None
    decision: str | None = None
    risk: str | None = None
    at: float | None = None
    cost: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in vars(self).items() if v not in (None, "")}


@dataclass(frozen=True)
class TaskTrace:
    task_id: uuid.UUID
    user_id: uuid.UUID
    graph_id: uuid.UUID | None
    kind: EvaluationKind
    mode: str
    user_request: str
    status: str
    failure_code: str | None
    final_response: str | None
    unresolved: bool
    counters: dict[str, int]
    elapsed_seconds: float
    stalls: int
    trips: tuple[str, ...]
    steps: tuple[TraceStep, ...]
    redactions: tuple[str, ...] = ()
    truncated: bool = False
    total_steps: int = 0
    # docs/29 §18: which agent run this task is (ids and version only).
    agent: dict[str, Any] | None = None

    @property
    def agent_id(self) -> uuid.UUID | None:
        return uuid.UUID(self.agent["agent_id"]) if self.agent else None

    def refs(self) -> set[str]:
        return {s.ref for s in self.steps}

    def for_judge(self) -> dict[str, Any]:
        """The trace as the Judge receives it: no user id, no graph id — the
        Judge evaluates a task, not a person."""

        return {
            "task_id": str(self.task_id),
            "evaluation_kind": self.kind.value,
            "mode": self.mode,
            "user_request": self.user_request,
            "status": self.status,
            "failure_code": self.failure_code,
            "final_response": self.final_response,
            "worker_reported_unresolved": self.unresolved,
            "counters": self.counters,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "stalls": self.stalls,
            "breaker_trips": list(self.trips),
            "steps": [s.as_dict() for s in self.steps],
            "steps_omitted": self.truncated,
            **({"agent_run": self.agent} if self.agent else {}),
        }

    def to_json(self) -> str:
        return json.dumps(self.for_judge(), ensure_ascii=False, sort_keys=True)


@dataclass
class _Redactor:
    max_chars: int
    hits: list[str] = field(default_factory=list)

    def __call__(self, text: str | None) -> str | None:
        if text is None:
            return None
        clean, names = redact_secrets(text)
        for name in names:
            if name not in self.hits:
                self.hits.append(name)
        if len(clean) > self.max_chars:
            clean = clean[: self.max_chars] + " [truncated]"
        return clean


def build_trace(source: TraceSource, *, kind: EvaluationKind, max_chars: int,
                window_steps: int | None = None) -> TaskTrace:
    """One task's trace — redacted, bounded, and (for a live window) the most
    recent `window_steps` steps. Step refs are numbered over the whole task, so
    a window's refs stay stable across evaluations."""

    if not isinstance(source, TraceSource):
        raise TypeError("a trace is built from exactly one task's TraceSource")
    clean = _Redactor(max_chars=max_chars)

    ordered: list[tuple[int, int, TraceStep]] = []
    for index, message in enumerate(source.messages):
        kind_ = "proposal" if message.role == "assistant" else "observation"
        ordered.append((index, 1, TraceStep(ref="", kind=kind_, text=clean(message.content) or "")))
    for event in source.events:
        position = max(0, min(event.position, len(source.messages)))
        ordered.append((position, 0, TraceStep(
            ref="", kind="event", name=event.name, result=event.result,
            resource=clean(event.resource), decision=event.decision, risk=event.risk,
            at=round(event.at, 3), cost=event.cost if event.cost else None,
        )))
    # Events recorded before message `position` was appended come first.
    ordered.sort(key=lambda item: (item[0], item[1]))
    steps = [
        TraceStep(**{**vars(step), "ref": f"s{number}"})
        for number, (_, _, step) in enumerate(ordered, start=1)
    ]
    total = len(steps)
    limit = min(MAX_STEPS, window_steps) if window_steps is not None else MAX_STEPS
    truncated = total > limit
    if truncated:
        steps = steps[-limit:]

    return TaskTrace(
        task_id=source.task_id,
        user_id=source.user_id,
        graph_id=source.graph_id,
        kind=kind,
        mode=source.mode,
        user_request=clean(source.user_request) or "",
        status=source.status,
        failure_code=source.failure_code,
        final_response=clean(source.final_response),
        unresolved=source.unresolved,
        counters={
            "iterations": source.iterations,
            "model_calls": source.model_calls,
            "tool_calls": source.tool_calls,
            "worker_switches": source.worker_switches,
            "denials": source.denials,
            "boundary_violations": source.violations,
            "confirmation_rejections": source.rejections,
        },
        elapsed_seconds=source.elapsed_seconds,
        stalls=sum(1 for e in source.events if e.name == "agent.stall.detected"),
        trips=tuple(t for t in (source.tripped_source,) if t),
        steps=tuple(steps),
        redactions=tuple(clean.hits),
        truncated=truncated,
        total_steps=total,
        agent=({"agent_id": str(source.agent_id), "run_id": str(source.agent_run_id),
                "version": source.agent_version} if source.agent_id is not None else None),
    )


__all__ = ["MAX_STEPS", "SourceEvent", "SourceMessage", "TaskTrace", "TraceSource", "TraceStep", "build_trace"]
