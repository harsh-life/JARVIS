"""Action-intent recognition: the contract (Phase 0.2, docs/31 §10).
Experiment-local; nothing in `server/` or `shared/` knows these types.

Until Phase 0.1 the harness learned that a task involved an action only
because the task fixture said so (`Task.actions`). Real requests carry no such
declaration, so a recognizer has to propose one. What it proposes is an
**interpretation, not a permission**:

* An `IntentResult` never authorizes anything and never reaches an operation.
  The only way an action enters the `ActionLedger` is `admit()` in
  `routing.py`, which is harness code, takes only validated candidates, and
  records them as *requested*, never as executed or confirmed.
* Candidates carry no status, execution, confirmation or verification field.
  `extra="forbid"` rejects any attempt to add one.
* Every piece of text a candidate carries that may later be shown to the user
  (`target`, `conditions`, `objective`) must be a **verbatim span of the
  user's own request** (`grounded_span`). A model-proposed target that is not
  in the request is dropped, and the target counts as unspecified, never
  guessed.
* Text inside quotes, and anything in the evidence, is never parsed as the
  user's instruction. It is recorded as a `QuotedInstruction` with its source.

Three top-level outcomes: `informational`, `action` and `ambiguous`. A
request can be an action *and* need clarification (an unspecified target);
`route` says what the response does: `answer`, `action_status` or `clarify`.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from experiments.actions import ActionKind
from experiments.suspicion import ActionSuspicion


class IntentClass(str, Enum):
    INFORMATIONAL = "informational"
    ACTION = "action"
    AMBIGUOUS = "ambiguous"


class RequestMode(str, Enum):
    EXECUTE = "execute"            # the user asks for the side effect
    ASSESS = "assess"              # "would deleting this be safe?"
    ADVISE = "advise"              # "how do I delete a file?"
    HYPOTHETICAL = "hypothetical"  # "imagine you paid it", fiction, role-play
    QUOTED = "quoted"              # an instruction someone else gave, or one in the evidence


class MissingSlot(str, Enum):
    TARGET = "target"              # which file / invoice / account
    AMOUNT = "amount"
    RECIPIENT = "recipient"
    TIME = "time"
    ACTION = "action"              # what the user wants done ("handle the invoice")
    DETAIL = "detail"
    CONDITION = "condition"        # Phase 0.3: a condition that does not say what is tested


Route = Literal["answer", "action_status", "clarify"]
Source = Literal["deterministic", "model", "hybrid"]
SubtaskKind = Literal["find", "check", "assess", "research", "compare", "report", "draft", "review", "summarize",
                      "calculate", "other"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Subtask(_M):
    """An informational step (find, assess, compare, draft…). Producing it has
    no side effect; drafting a message is not sending it."""

    subtask_id: str = Field(pattern=r"^s[0-9]{1,2}$")
    kind: SubtaskKind
    text: str


class ActionCandidate(_M):
    """One side effect the request may be asking for, as interpreted."""

    candidate_id: str = Field(pattern=r"^c[0-9]{1,2}$")   # assigned by the harness, in order of appearance
    action_type: ActionKind
    verb: str                                             # the requested verb, lemmatized ("pay", "delete")
    objective: str                                        # the request's own words for it
    target: str | None = None                             # a verbatim span of the request, or None
    target_specified: bool = False
    constraints: tuple[str, ...] = ()                     # "right now", "from my savings"
    conditions: tuple[str, ...] = ()                      # the user's own words: "the invoice is valid"
    conditional: bool = False
    requires_user_approval: bool = False                  # "only after I approve"
    depends_on: tuple[str, ...] = ()                      # subtask / candidate ids
    mode: RequestMode = RequestMode.EXECUTE
    evidence_refs: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    needs_clarification: bool = False
    # Phase 0.3 (rules v2). Where the target came from (explicit, unique,
    # chain, condition_subject, evidence), whether the request wrote it as a
    # pronoun, each condition's kind, and whether every condition is complete.
    target_provenance: str | None = None
    pronoun_target: bool = False
    condition_kinds: tuple[str, ...] = ()
    conditions_complete: bool = True

    @model_validator(mode="after")
    def _consistent(self) -> "ActionCandidate":
        if self.conditional != bool(self.conditions) and self.conditions_complete:
            raise ValueError("a candidate is conditional exactly when it has conditions")
        if self.condition_kinds and len(self.condition_kinds) != len(self.conditions):
            raise ValueError("one kind per condition")
        if self.mode is RequestMode.EXECUTE and not self.conditions_complete and not self.needs_clarification:
            raise ValueError("an incomplete condition needs clarification, not a guess")
        if self.target_specified and not self.target:
            raise ValueError("a specified target needs the target text")
        if self.mode is RequestMode.EXECUTE and not self.target_specified and not self.needs_clarification:
            raise ValueError("an executable request with an unspecified target needs clarification, not a guess")
        return self


class QuotedInstruction(_M):
    text: str
    source: Literal["prompt_quote", "reported", "evidence"]
    evidence_ref: str | None = None


class Missing(_M):
    candidate_id: str | None
    slot: MissingSlot


class IntentResult(_M):
    classification: IntentClass
    candidates: tuple[ActionCandidate, ...] = ()
    subtasks: tuple[Subtask, ...] = ()
    quoted_instructions: tuple[QuotedInstruction, ...] = ()
    missing: tuple[Missing, ...] = ()
    uncertainties: tuple[str, ...] = ()
    source: Source
    # Model and hybrid only: what was rejected from the model's proposal and why.
    rejected: tuple[str, ...] = ()
    # Phase 0.3: the routing-only side-effect signal (never a ledger input).
    suspicion: ActionSuspicion | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "IntentResult":
        execute = [c for c in self.candidates if c.mode is RequestMode.EXECUTE]
        if self.classification is IntentClass.ACTION and not execute:
            raise ValueError("an action classification needs at least one executable candidate")
        if self.classification is IntentClass.INFORMATIONAL and execute:
            raise ValueError("an informational classification has no executable candidate")
        if self.classification is IntentClass.AMBIGUOUS and not (self.missing or self.uncertainties):
            raise ValueError("an ambiguous classification says what is uncertain")
        ids = [c.candidate_id for c in self.candidates] + [s.subtask_id for s in self.subtasks]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate candidate or subtask id")
        for c in self.candidates:
            if set(c.depends_on) - set(ids) or c.candidate_id in c.depends_on:
                raise ValueError(f"{c.candidate_id} depends on an unknown id or on itself")
        return self

    @property
    def executable(self) -> tuple[ActionCandidate, ...]:
        return tuple(c for c in self.candidates if c.mode is RequestMode.EXECUTE)

    @property
    def route(self) -> Route:
        if self.classification is IntentClass.AMBIGUOUS:
            return "clarify"
        if self.classification is IntentClass.ACTION:
            return "clarify" if any(c.needs_clarification for c in self.executable) else "action_status"
        return "answer"


_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", text.replace("’", "'").replace("“", '"').replace("”", '"')).strip().lower()


def grounded_span(candidate: str | None, request: str) -> str | None:
    """`candidate` if it is a verbatim span of `request` (whitespace, case
    and quote style aside), else None. Returned as written in the request."""

    if not candidate or not candidate.strip():
        return None
    needle = _norm(candidate).strip(" .,;:!?\"'")
    hay = _norm(request)
    if len(needle) < 2 or needle not in hay:
        return None
    # Return the user's own casing.
    flat = _WS.sub(" ", request.replace("’", "'").replace("“", '"').replace("”", '"'))
    idx = flat.lower().find(needle)
    return flat[idx: idx + len(needle)] if idx >= 0 else needle


__all__ = ["ActionCandidate", "IntentClass", "IntentResult", "Missing", "MissingSlot", "QuotedInstruction",
           "RequestMode", "Route", "Subtask", "grounded_span"]
