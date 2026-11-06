"""Condition analysis (Phase 0.3, docs/31 §11.5). Experiment-local.

A non-empty string is not necessarily a condition. A condition is usable only
if it has an identifiable subject and a predicate that says what is tested.
Each condition is classified as one of:

* `factual`: a fact about the world ("the balance is above 2,000 credits").
* `temporal`: an event to wait for ("once the upload finishes").
* `user_step`: something the user will do ("once I've checked it"). It is
  **not** approval. Checking, reading or reviewing is flagged
  `ambiguous_approval` so the response can say that it waits for the step
  without treating the step as consent.
* `approval`: an explicit approval requirement ("only after I approve", "after
  I give the go-ahead"). It is carried as `requires_user_approval`, not as a
  condition.
* `vague`: "if needed", "if it's okay", "when it makes sense", "if it is".
  These are incomplete, and the request is clarified.

None of these authorizes anything. A condition that holds makes an action at
most *eligible to be proposed*: the ledger still needs its own condition
record, and any future production integration would still apply its risk,
authorization and confirmation rules.

An elliptical condition ("…check whether it is legitimate, and pay it if it
is") may be **recovered** from an earlier clause of the same request, with
provenance, only if that clause has the same subject and auxiliary and a
complement. Otherwise it stays incomplete.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Sequence

ConditionKind = Literal["factual", "temporal", "user_step", "approval", "vague"]

TEMPORAL_CONNECTORS = {"once", "when", "after", "as soon as", "before"}
APPROVAL_STEP = re.compile(
    r"^(?:i|we)(?:'ve|\s+have|\s+had)?\s+(?:approve[ds]?|confirm(?:ed|s)?|give (?:you )?(?:the |my )?(?:go-ahead|"
    r"ok|okay|green light|nod)|gave (?:you )?(?:the |my )?(?:go-ahead|ok|okay|green light)|say so|said so|sign off|"
    r"signed off|okay it|ok it|okayed it)\b", re.I)
USER_STEP = re.compile(r"^(?:i|we)(?:'ve|\s+have|\s+had|'ll|\s+will)?\s+(?:just\s+)?(?P<verb>[a-z]+)\b", re.I)
CHECKING_VERBS = {"check", "checked", "checks", "read", "reads", "review", "reviewed", "reviews", "look", "looked",
                  "looks", "see", "seen", "go", "gone", "went", "verify", "verified", "inspect", "inspected",
                  "proofread", "glance", "glanced", "skim", "skimmed"}
SUBJECT_PRONOUN = re.compile(r"^(?:it|they|he|she|this|that|these|those|its|it's|they're|he's|she's)\b", re.I)
# A predicate that says nothing testable.
VAGUE = re.compile(
    r"^(?:(?:it|that|this|they|he|she|so)\s*)?(?:'s|'re|is|are|was|were|does|do|did|seems?|looks?)?\s*"
    r"(?:ok(?:ay)?|fine|alright|all right|needed|necessary|required|appropriate|possible|relevant|sensible|"
    r"reasonable|wise|good|right|ready|time|worth it|best|better|helpful|useful|applicable|so|"
    r"makes? sense|you think so|i think so|something'?s off|anything'?s off|something is off|"
    r"there'?s a problem|there is a problem|it works|works)?\s*$", re.I)
COPULA_ONLY = re.compile(r"^(?:it|that|this|they|he|she)\s*(?:'s|'re|is|are|was|were|does|do|did|has|have|will|"
                         r"can|would|could)\s*$", re.I)


@dataclass(frozen=True)
class Condition:
    text: str                    # verbatim from the request (or recovered verbatim span)
    connector: str               # if / unless / once / when / after / …
    kind: ConditionKind
    complete: bool
    subject: str | None = None
    pronoun_subject: bool = False
    recovered_from: str | None = None
    ambiguous_approval: bool = False
    reason: str | None = None


def analyze(text: str, connector: str, *, earlier_clauses: Sequence[tuple[str, str]] = ()) -> Condition:
    """`earlier_clauses` are (id, clause text) of the same request, used only
    to recover an elliptical condition, with provenance."""

    t = text.strip().strip(" ,.")
    low = t.lower()
    conn = connector.lower()
    if APPROVAL_STEP.match(low):
        return Condition(t, conn, "approval", True)
    m = USER_STEP.match(low)
    if m and m.group("verb") not in {"is", "are", "was", "were", "do", "does"}:
        return Condition(t, conn, "user_step", True, subject=t.split()[0],
                         ambiguous_approval=m.group("verb") in CHECKING_VERBS)
    if COPULA_ONLY.match(low):
        aux = low.split()[-1] if " " in low else low[-2:]
        pron = re.match(r"^(it|that|this|they|he|she)", low).group(1)
        for cid, clause in reversed(list(earlier_clauses)):
            r = re.search(rf"\b(?:whether|if)\s+({pron}\s+(?:is|are|was|were|does|do|has|have)\s+[^,.;]+)", clause, re.I)
            if r and len(r.group(1).split()) >= 3:
                return Condition(r.group(1).strip(), conn, "factual", True, subject=pron, pronoun_subject=True,
                                 recovered_from=cid)
        return Condition(t, conn, "vague", False, subject=pron, pronoun_subject=True,
                         reason="the condition has no predicate")
    if VAGUE.match(low):
        return Condition(t, conn, "vague", False, reason="the condition does not say what is tested")
    kind: ConditionKind = "temporal" if conn in TEMPORAL_CONNECTORS else "factual"
    words = low.split()
    if len(words) < 2:
        return Condition(t, conn, "vague", False, reason="the condition is a single word")
    return Condition(t, conn, kind, True, subject=words[0], pronoun_subject=bool(SUBJECT_PRONOUN.match(low)))


__all__ = ["Condition", "ConditionKind", "analyze"]
