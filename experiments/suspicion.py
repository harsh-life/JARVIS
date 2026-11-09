"""Action suspicion (Phase 0.3, docs/31 §11.2). Experiment-local.

An `ActionSuspicion` records that a request *may* ask for a side effect,
separately from whether a complete `ActionCandidate` could be built. It is
not a candidate, an authorization decision, a capability grant, an execution
record or a confirmation, and nothing writes it into the `ActionLedger`. It
has one use: **routing**. A request with credible side-effect language from
the user that could not be turned into a complete, resolved candidate goes to
`clarify`, not to the screened `answer` route.

Confidence is expressed as typed reasons, never as one opaque score.
Language is *credible* only when it is the user's own instruction. Side-effect
words inside advice and how-to questions, hypotheticals, fiction, quotes,
reported instructions and the evidence are recorded with that origin and are
not credible, so a discussion of an action does not become a clarification.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class SuspicionReason(str, Enum):
    INDIRECT_ACTION_LANGUAGE = "indirect_action_language"        # "I need X paid", "make sure X reaches Y"
    PASSIVE_ACTION_REQUEST = "passive_action_request"            # "X should be refunded", "X should go out"
    OUTCOME_REQUESTED = "outcome_requested"                      # "I want the files gone"
    IMPERATIVE_ACTION_UNRESOLVED = "imperative_action_unresolved"  # an imperative whose verb is not known
    UNRESOLVED_TARGET = "unresolved_target"                      # "the thing we talked about"
    UNRESOLVED_PRONOUN = "unresolved_pronoun"                    # "cancel it" with no antecedent
    MULTIPLE_ANTECEDENTS = "multiple_antecedents"                # "it" could be either of two things
    INCOMPLETE_CONDITION = "incomplete_condition"                # "if it is", "if needed"
    AMBIGUOUS_APPROVAL = "ambiguous_approval"                    # "once I've checked it": a step, not approval
    RECOGNIZER_DISAGREEMENT = "recognizer_disagreement"          # rules and model read it differently
    POSSIBLE_SIDE_EFFECT_WITHOUT_CANDIDATE = "possible_side_effect_without_candidate"


Origin = Literal["user_instruction", "advice", "hypothetical", "fiction", "quoted", "reported", "evidence"]
SignalKind = Literal["explicit", "indirect", "passive", "outcome", "unknown_verb"]

# Reasons that, on their own, make a request unfit for the action-status
# route (it must be clarified).
BLOCKING = frozenset({SuspicionReason.IMPERATIVE_ACTION_UNRESOLVED, SuspicionReason.UNRESOLVED_TARGET,
                      SuspicionReason.UNRESOLVED_PRONOUN, SuspicionReason.MULTIPLE_ANTECEDENTS,
                      SuspicionReason.INCOMPLETE_CONDITION, SuspicionReason.RECOGNIZER_DISAGREEMENT,
                      SuspicionReason.POSSIBLE_SIDE_EFFECT_WITHOUT_CANDIDATE})


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SuspicionSignal(_M):
    span: str                 # the words, as written
    origin: Origin
    kind: SignalKind


class ActionSuspicion(_M):
    signals: tuple[SuspicionSignal, ...] = ()
    candidate_extracted: bool = False
    target_resolved: bool | None = None      # None: no candidate to resolve
    condition_complete: bool | None = None   # None: no condition
    reasons: tuple[SuspicionReason, ...] = ()

    @property
    def side_effect_language(self) -> bool:
        return bool(self.signals)

    @property
    def user_originated(self) -> bool:
        return any(s.origin == "user_instruction" for s in self.signals)

    @property
    def explicit_request(self) -> bool:
        return any(s.origin == "user_instruction" and s.kind == "explicit" for s in self.signals)

    @property
    def indirect_request(self) -> bool:
        return any(s.origin == "user_instruction" and s.kind in ("indirect", "passive") for s in self.signals)

    @property
    def outcome_implied(self) -> bool:
        return any(s.origin == "user_instruction" and s.kind == "outcome" for s in self.signals)

    @property
    def level(self) -> Literal["none", "framed", "credible"]:
        if self.user_originated:
            return "credible"
        return "framed" if self.signals else "none"

    @property
    def ambiguous(self) -> bool:
        return any(r in BLOCKING for r in self.reasons)

    def summary(self) -> dict:
        return {"level": self.level, "user_originated": self.user_originated, "explicit": self.explicit_request,
                "indirect": self.indirect_request, "outcome": self.outcome_implied,
                "candidate_extracted": self.candidate_extracted, "target_resolved": self.target_resolved,
                "condition_complete": self.condition_complete, "ambiguous": self.ambiguous,
                "reasons": [r.value for r in self.reasons],
                "signals": [s.model_dump() for s in self.signals]}


__all__ = ["BLOCKING", "ActionSuspicion", "Origin", "SignalKind", "SuspicionReason", "SuspicionSignal"]
