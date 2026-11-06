"""Referent resolution (Phase 0.3, docs/31 §11.4). Experiment-local.

Only the supplied context is used: the request itself, and the evidence the
harness supplied. A reference to an earlier conversation the harness cannot
see ("the thing we talked about", "the same as last time", "the other one")
is unresolved, however definite it sounds.

Rules:

* **Action targets and condition references are kept apart.** A pronoun in a
  condition ("if *it* renews") is never itself a target. A condition's
  subject is used as the action's target only if it is a full noun phrase
  ("If *invoice 7720* matches the order, pay it"), and that use is recorded
  with provenance `condition_subject`.
* **No silent guess between candidates.** With exactly one antecedent, a
  pronoun resolves to it (`unique`). With several, it resolves only in a
  coordinated chain whose immediately preceding step introduced the most
  recent one ("draft a reply, and send it") (`chain`). Otherwise it is
  unresolved (`multiple`), and the request is clarified.
* Every resolution records where it came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

PRONOUNS = re.compile(r"^(?:it|this|that|them|these|those|they|this one|that one|the same|one|everything|all of "
                      r"(?:it|them)|him|her|hers|his|theirs|mine|ours)$", re.I)
CONTEXT_REFERENCE = re.compile(
    r"\b(?:we|you|i|they)\s+(?:talked|spoke|discussed|mentioned|agreed|said|chatted|looked at|saw)\b|"
    r"\b(?:the usual|as usual|same as (?:last time|before)|the same as|like last time|last time|as before|"
    r"the other one|the other|another one of (?:those|these|them)|one of those|one of these|"
    r"what (?:i|we) owe|whatever|you know which|the one i (?:mentioned|meant|said))\b", re.I)
NP = re.compile(
    r"\b(?:(?:my|our|your|his|her|their|the|a|an|this|that|these|those|all|every|each)\s+)"
    r"(?:[\w'.-]+\s+){0,5}?[\w'.-]+(?=\s+(?:is|are|was|were|has|have|had|renews?|expires?|ends?|starts?|matches|match|"
    r"exceeds|arrives?|comes?|goes|will|would|can|could|should|from|to|for|in|on|at|by|with|and|or|if|when|that|"
    r"which|who)\b|[,.?!;]|$)", re.I)

Status = Literal["explicit", "unique", "chain", "condition_subject", "evidence", "no_antecedent", "multiple",
                 "context_reference"]


@dataclass
class Entity:
    id: str
    text: str            # verbatim
    source: str          # the step it came from (s1, c2, reported, condition)
    clause: int


@dataclass
class Resolution:
    text: str | None
    status: Status
    provenance: str | None = None
    candidates: list[str] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.status in ("explicit", "unique", "chain", "condition_subject", "evidence")


def is_pronoun(text: str | None) -> bool:
    return not text or bool(PRONOUNS.match(text.strip().lower()))


def is_context_reference(text: str | None) -> bool:
    return bool(text and CONTEXT_REFERENCE.search(text))


def first_np(text: str) -> str | None:
    """The first noun phrase of a phrase ("when my Ridgeway gym contract renews" → "my Ridgeway gym contract")."""

    t = re.sub(r"^(?:when|whether|if|what|how|which|where|why|who)\s+", "", text.strip(), flags=re.I)
    m = NP.search(t)
    if not m:
        return None
    np_ = m.group(0).strip()
    return None if is_pronoun(np_) else np_


@dataclass
class Discourse:
    entities: list[Entity] = field(default_factory=list)

    def add(self, text: str | None, source: str, clause: int) -> None:
        if text and not is_pronoun(text) and not is_context_reference(text):
            if not any(e.text.lower() == text.lower() for e in self.entities):
                self.entities.append(Entity(f"e{len(self.entities) + 1}", text, source, clause))

    def resolve(self, target: str | None, *, clause: int, in_chain: bool, condition_subject: str | None,
                evidence_ids: tuple[str, ...] = (), refers_to_evidence: bool = False) -> Resolution:
        if target and is_context_reference(target):
            return Resolution(None, "context_reference")
        if target and not is_pronoun(target):
            return Resolution(target, "explicit", "request")
        earlier = [e for e in self.entities if e.clause < clause]
        distinct = list({e.text.lower(): e for e in earlier}.values())
        if len(distinct) == 1:
            return Resolution(distinct[0].text, "unique", distinct[0].source, [distinct[0].text])
        if len(distinct) > 1:
            last = distinct[-1]
            if in_chain and last.clause == clause - 1:
                return Resolution(last.text, "chain", last.source, [e.text for e in distinct])
            return Resolution(None, "multiple", None, [e.text for e in distinct])
        if condition_subject and not is_pronoun(condition_subject):
            return Resolution(condition_subject, "condition_subject", "condition")
        if refers_to_evidence and evidence_ids:
            return Resolution(None, "evidence", ",".join(evidence_ids))
        return Resolution(None, "no_antecedent")


__all__ = ["CONTEXT_REFERENCE", "Discourse", "Entity", "Resolution", "first_np", "is_context_reference",
           "is_pronoun"]
