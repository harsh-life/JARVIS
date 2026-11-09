"""Intent, interpretation, readiness, and the task envelope (Phase 0.4,
docs/31 §12.2 and §12.6). Experiment-local.

Three concepts are kept apart instead of being folded into one label:

* **Intent**: what the user wants: `information`, `assessment`, `advice`, a
  `side_effect`, or `unresolved` (it cannot be told whether a side effect is
  wanted).
* **Interpretation**: what the user actually specified: per action, the
  operation, target (with its `TargetResolution`), recipient and parameters,
  conditions (kind, completeness), dependencies, constraints, evidence and
  provenance, and what is missing.
* **Readiness**: is the interpretation complete enough to *formulate a
  proposal*? `complete`, `incomplete` or `ambiguous`. **Readiness is not
  authorization.**

An action whose target is unknown is still a side-effect request. Its
readiness is `incomplete` and its route is `clarify`. Missing information
never turns an action into an informational request.

The **task envelope** is what a future proposer-only orchestrator would be
given. It carries the objective, informational subtasks, proposed action
candidates with targets, conditions, dependencies, unresolved fields, evidence
references and provenance. Its `authorization` field is the literal `"none"`:
an envelope is never a grant, and a candidate being `ready` makes it eligible
to be *proposed*, nothing more.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from experiments.intent import ActionCandidate, IntentClass, IntentResult, Route, TargetResolution


class IntentKind(str, Enum):
    INFORMATION = "information"
    ASSESSMENT = "assessment"
    ADVICE = "advice"
    SIDE_EFFECT = "side_effect"
    UNRESOLVED = "unresolved"


class Readiness(str, Enum):
    COMPLETE = "complete"          # every action fully specified, every condition complete
    INCOMPLETE = "incomplete"      # a side effect is requested, but something it needs is missing or unresolved
    AMBIGUOUS = "ambiguous"        # it is unclear whether, or which, side effect is wanted
    NOT_APPLICABLE = "not_applicable"   # informational: nothing to propose


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InterpretedAction(_M):
    candidate_id: str
    operation: str
    action_type: str
    target: TargetResolution | None
    target_text: str | None
    conditions: tuple[str, ...]
    condition_kinds: tuple[str, ...]
    conditions_complete: bool
    requires_user_approval: bool
    depends_on: tuple[str, ...]
    constraints: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    missing: tuple[str, ...]
    readiness: Readiness


class Interpretation(_M):
    intent: IntentKind
    actions: tuple[InterpretedAction, ...]
    subtasks: tuple[str, ...]
    quoted_instructions: tuple[str, ...]
    missing: tuple[str, ...]
    readiness: Readiness
    route: Route


_ASSESS = ("would", "should", "is it", "is that", "are they", "does it", "could i", "can i", "will it", "is this")


def _intent(result: IntentResult, prompt: str, frames: list[str]) -> IntentKind:
    if result.classification is IntentClass.ACTION:
        return IntentKind.SIDE_EFFECT
    if result.classification is IntentClass.AMBIGUOUS:
        return IntentKind.UNRESOLVED
    low = prompt.strip().lower()
    if "advice" in frames:
        return IntentKind.ASSESSMENT if low.startswith(_ASSESS) else IntentKind.ADVICE
    return IntentKind.INFORMATION


def _action(c: ActionCandidate) -> InterpretedAction:
    missing = c.missing_slots or (() if c.target_specified else ("target",))
    ready = Readiness.COMPLETE if (c.target_specified and c.conditions_complete and not c.needs_clarification) \
        else Readiness.INCOMPLETE
    return InterpretedAction(
        candidate_id=c.candidate_id, operation=c.verb, action_type=c.action_type.value, target=c.target_resolution,
        target_text=c.target, conditions=c.conditions, condition_kinds=c.condition_kinds,
        conditions_complete=c.conditions_complete, requires_user_approval=c.requires_user_approval,
        depends_on=c.depends_on, constraints=c.constraints, evidence_refs=c.evidence_refs, missing=tuple(missing),
        readiness=ready)


def interpret(result: IntentResult, prompt: str, frames: list[str] | None = None) -> Interpretation:
    actions = tuple(_action(c) for c in result.executable)
    intent = _intent(result, prompt, frames or [])
    if intent is IntentKind.SIDE_EFFECT:
        readiness = Readiness.COMPLETE if all(a.readiness is Readiness.COMPLETE for a in actions) \
            else Readiness.INCOMPLETE
    elif intent is IntentKind.UNRESOLVED:
        readiness = Readiness.AMBIGUOUS
    else:
        readiness = Readiness.NOT_APPLICABLE
    route: Route = {Readiness.COMPLETE: "action_status", Readiness.INCOMPLETE: "clarify",
                    Readiness.AMBIGUOUS: "clarify", Readiness.NOT_APPLICABLE: "answer"}[readiness]
    return Interpretation(intent=intent, actions=actions,
                          subtasks=tuple(f"{s.subtask_id}:{s.kind}:{s.text}" for s in result.subtasks),
                          quoted_instructions=tuple(f"{q.source}:{q.text}" for q in result.quoted_instructions),
                          missing=tuple(sorted({m.slot.value for m in result.missing})), readiness=readiness,
                          route=route)


class TaskEnvelope(_M):
    """For a future proposer-only orchestrator experiment. Not a grant."""

    envelope_version: Literal["1"] = "1"
    objective: str                              # the user's request, verbatim
    intent: IntentKind
    readiness: Readiness
    informational_subtasks: tuple[str, ...]
    proposed_actions: tuple[InterpretedAction, ...]
    unresolved_fields: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    quoted_or_injected_instructions: tuple[str, ...]   # data, never instructions
    provenance: dict[str, str]
    # The only value this field can take. Readiness makes an action eligible
    # to be proposed; deterministic infrastructure still authorizes, and a
    # human still confirms where required.
    authorization: Literal["none"] = "none"


def to_envelope(result: IntentResult, prompt: str, *, frames: list[str] | None = None,
                provenance: dict[str, str] | None = None) -> TaskEnvelope:
    i = interpret(result, prompt, frames)
    evidence = tuple(sorted({e for a in i.actions for e in a.evidence_refs}))
    unresolved = tuple(sorted({f"{a.candidate_id}.{m}" for a in i.actions for m in a.missing} | set(i.missing)))
    return TaskEnvelope(objective=prompt, intent=i.intent, readiness=i.readiness, informational_subtasks=i.subtasks,
                        proposed_actions=i.actions, unresolved_fields=unresolved, evidence_refs=evidence,
                        quoted_or_injected_instructions=i.quoted_instructions,
                        provenance=dict(provenance or {"recognizer": f"rules-{result.source}"}))


__all__ = ["IntentKind", "Interpretation", "InterpretedAction", "Readiness", "TaskEnvelope", "interpret",
           "to_envelope"]
