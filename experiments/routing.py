"""Response coverage policy (Phase 0.2, docs/31 §10.5). Experiment-local.

One routing decision per request, from the `IntentResult`:

* **answer** (informational): the model's answer is the response, screened
  sentence by sentence (`finalizer.finalize` with an empty ledger). That
  screen is lexical and misses about a third of paraphrased claims on a
  held-out corpus (docs/31 §9.6): **it is not a truthfulness guarantee.**
* **action_status** (action): the response is built only by harness code.
  Per action: the ledger's status sentence, then explanation sentences
  rendered from templates (`explanations.py`), whether derived by the harness
  or model-proposed and validated. Then any evidence-anchored facts that
  validated. No model prose at all.
* **clarify** (ambiguous, or an action whose target or a needed detail is
  missing): a deterministic clarification. "Nothing has been done" and one
  question per missing slot. It never claims an action, and never reads as a
  refusal: uncertainty is worded differently from denial, prohibition, lack
  of capability and failure.

`admit` is the only path from an intent to the `ActionLedger`. It is harness
code; it takes only executable, fully specified candidates on the
`action_status` route; it records them as *requested*. A candidate is data:
it cannot write to a ledger, create a confirmation, a condition check or an
execution, or reuse another action's record.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Literal, Sequence

from experiments.actions import ActionLedger, ActionOutcome, ActionSpec, ActionStatus, ExecutionFixture, apply_fixture
from experiments.contracts import EvidenceItem
from experiments.explanations import Explanation, clarification_questions, derived, validate_facts, validate_proposed
from experiments.finalizer import Finalized, finalize, status_line
from experiments.intent import ActionCandidate, IntentResult, Route

CLARIFY_LEAD = "Nothing has been done, because I'm not sure yet exactly what you want."


def describe(c: ActionCandidate) -> str:
    """The ledger description: the requested verb (closed vocabulary) and the
    target in the user's own words, in the second person."""

    target = c.target or "it"
    target = re.sub(r"\bmy\b", "your", target, flags=re.I)
    target = re.sub(r"\bme\b", "you", target, flags=re.I)
    text = f"{c.verb} {target}"
    if c.requires_user_approval:
        text += " (after your approval)"
    return text[:200]


def admit(ledger: ActionLedger, intent: IntentResult, *,
          fixture: ExecutionFixture | None = None) -> dict[str, str]:
    """Record the request's executable candidates as requested actions. →
    {candidate_id: action_id}. Nothing is admitted unless the route is
    `action_status`. Without a fixture each is `not_available` (the harness
    has no tools); with one, the scripted history is applied by the harness."""

    if intent.route != "action_status":
        return {}
    admitted: dict[str, str] = {}
    for c in intent.executable:
        if not c.target_specified or c.needs_clarification:
            continue
        kind = c.condition_kinds[0] if c.condition_kinds else None
        spec = ActionSpec(kind=c.action_type, description=describe(c),
                          condition=c.conditions[0][:200] if c.conditional else None,
                          condition_kind=kind if kind in ("factual", "temporal", "user_step") else None)
        if fixture is not None:
            admitted[c.candidate_id] = apply_fixture(ledger, spec, fixture)
        elif c.conditional:
            a = ledger.request(spec, status=ActionStatus.PROPOSED, reason="conditional")
            ledger.transition(a, ActionStatus.NOT_EXECUTED, reason="condition_not_evaluated")
            admitted[c.candidate_id] = a
        else:
            admitted[c.candidate_id] = ledger.request(spec, status=ActionStatus.NOT_AVAILABLE,
                                                      reason="harness_has_no_tools")
    return admitted


@dataclass
class Section:
    kind: Literal["status", "explanation", "fact", "clarification", "answer"]
    text: str
    source: Literal["harness", "model"]
    code: str | None = None


@dataclass
class RoutedResponse:
    route: Route
    text: str
    sections: list[Section] = field(default_factory=list)
    admitted: dict[str, str] = field(default_factory=dict)
    rejected_codes: list[dict] = field(default_factory=list)
    rejected_facts: list[dict] = field(default_factory=list)
    finalization: dict | None = None

    def as_dict(self) -> dict:
        d = asdict(self)
        d["sections"] = [asdict(s) for s in self.sections]
        return d


def respond(intent: IntentResult, ledger: ActionLedger, *, model_answer: str | None = None,
            proposed_codes: Sequence = (), proposed_facts: Sequence = (), evidence: Sequence[EvidenceItem] = (),
            context: str = "", fixture: ExecutionFixture | None = None) -> RoutedResponse:
    route = intent.route
    if route == "answer":
        final: Finalized = finalize(model_answer, ActionLedger("answer"), context=context, parsed=model_answer is not None)
        return RoutedResponse(route=route, text=final.text,
                              sections=[Section("answer", s.text, s.source) for s in final.segments],
                              finalization=final.as_dict())

    if route == "clarify":
        sections = [Section("clarification", CLARIFY_LEAD, "harness")]
        sections += [Section("clarification", q, "harness") for q in clarification_questions(intent)]
        v = validate_proposed(proposed_codes, intent, {}, evidence)
        sections += [Section("explanation", e.text, "harness", e.code.value) for e in v.accepted
                     if e.code.value in ("evidence_insufficient", "quoted_instruction_ignored")]
        return RoutedResponse(route=route, text=" ".join(s.text for s in sections), sections=sections,
                              rejected_codes=v.rejected)

    admitted = admit(ledger, intent, fixture=fixture)
    outcomes: dict[str, ActionOutcome] = {}
    by_action = {o.action_id: o for o in ledger.outcomes()}
    for cid, aid in admitted.items():
        outcomes[cid] = by_action[aid]
    base = derived(intent, outcomes)
    v = validate_proposed(proposed_codes, intent, outcomes, evidence, already=base)
    facts = validate_facts(proposed_facts, evidence, intent)
    sections: list[Section] = []
    explanations: list[Explanation] = base + v.accepted
    for cid, outcome in outcomes.items():
        sections.append(Section("status", status_line(outcome), "harness"))
        sections += [Section("explanation", e.text, "harness", e.code.value) for e in explanations
                     if e.candidate_id == cid]
    sections += [Section("explanation", e.text, "harness", e.code.value) for e in explanations if e.candidate_id is None]
    sections += [Section("fact", f.render(), "harness") for f in facts.facts]
    text = "\n".join(s.text for s in sections)
    return RoutedResponse(route=route, text=text, sections=sections, admitted=admitted, rejected_codes=v.rejected,
                          rejected_facts=facts.rejected)


__all__ = ["CLARIFY_LEAD", "RoutedResponse", "Section", "admit", "describe", "respond"]
