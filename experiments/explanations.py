"""Structured explanations (Phase 0.2, docs/31 §10.4). Experiment-local.

Phase 0.1 withheld all model prose on action-intent tasks, which also lost
the useful parts (for example, "submit the form yourself"). This module
returns some of that safely, without letting prose back in:

* **Reason codes, rendered from templates.** Every explanation is a fixed
  sentence chosen by an `ExplanationCode`. Its slots are filled only from
  trusted state: the ledger's outcome, the grounded candidate (the user's own
  words), the evidence ids the harness supplied, and closed vocabularies (a
  missing-slot name, a verb lemma). No slot ever takes model text.
* **Status codes are the harness's.** `status_code(outcome)` derives one from
  the `ActionLedger`. A model may *propose* a status code, but it is accepted
  only if it equals the derived one, so it can never state a status the
  ledger does not hold.
* **Advisory codes are validated.** A model may propose `target_missing`,
  `information_required`, `action_unclear`, `evidence_insufficient` or
  `quoted_instruction_ignored`. Each has a precondition checked against the
  task and the intent; a code whose precondition fails is rejected and
  recorded, never shown.
* **Evidence-anchored facts.** To keep useful research content (an amount, a
  due date) without prose, a model may propose `{field, value, evidence_ref}`.
  It is shown only if the field is in a closed vocabulary (no "status" or
  "authorization"), the value occurs verbatim in that evidence item, the
  value is not inside an instruction found in the evidence, and the screen
  finds no claim in it. It is rendered as a quotation of the evidence ("From
  evidence I3: amount: 480 credits."), which is a claim about what the
  evidence says, not about the world.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

from experiments.actions import ActionKind, ActionOutcome, ActionStatus
from experiments.claims import analyze
from experiments.contracts import EvidenceItem
from experiments.intent import ActionCandidate, IntentResult, MissingSlot


class ExplanationCode(str, Enum):
    # derived from the ledger (status)
    ACTION_NOT_AVAILABLE = "action_not_available"
    NO_OPERATION = "no_operation"
    NOT_AUTHORIZED = "not_authorized"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CONFIRMATION_UNAVAILABLE = "confirmation_unavailable"
    PERMISSION_DENIED = "permission_denied"
    PROHIBITED = "prohibited"
    EXECUTION_FAILED = "execution_failed"
    PARTIALLY_COMPLETED = "partially_completed"
    EXECUTED_UNVERIFIED = "executed_unverified"
    VERIFICATION_FAILED = "verification_failed"
    VERIFIED = "verified"
    CONDITION_NOT_EVALUATED = "condition_not_evaluated"
    CONDITION_NOT_MET = "condition_not_met"
    # derived from the request (advisory)
    TARGET_MISSING = "target_missing"
    INFORMATION_REQUIRED = "information_required"
    ACTION_UNCLEAR = "action_unclear"
    APPROVAL_REQUESTED = "approval_requested"
    DEPENDS_ON_EARLIER_STEP = "depends_on_earlier_step"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    QUOTED_INSTRUCTION_IGNORED = "quoted_instruction_ignored"


STATUS_CODES = frozenset({
    ExplanationCode.ACTION_NOT_AVAILABLE, ExplanationCode.NO_OPERATION, ExplanationCode.NOT_AUTHORIZED,
    ExplanationCode.CONFIRMATION_REQUIRED, ExplanationCode.CONFIRMATION_UNAVAILABLE,
    ExplanationCode.PERMISSION_DENIED, ExplanationCode.PROHIBITED, ExplanationCode.EXECUTION_FAILED,
    ExplanationCode.PARTIALLY_COMPLETED, ExplanationCode.EXECUTED_UNVERIFIED, ExplanationCode.VERIFICATION_FAILED,
    ExplanationCode.VERIFIED, ExplanationCode.CONDITION_NOT_EVALUATED, ExplanationCode.CONDITION_NOT_MET,
})
# What a model may propose (anything else it proposes is rejected outright).
MODEL_PROPOSABLE = frozenset({
    ExplanationCode.TARGET_MISSING, ExplanationCode.INFORMATION_REQUIRED, ExplanationCode.ACTION_UNCLEAR,
    ExplanationCode.EVIDENCE_INSUFFICIENT, ExplanationCode.QUOTED_INSTRUCTION_IGNORED,
}) | STATUS_CODES

_NOUN = {ActionKind.PAYMENT: "bill or payee", ActionKind.MESSAGE: "message and recipient",
         ActionKind.FILE_DELETION: "file or folder", ActionKind.INVOICE_APPROVAL: "invoice",
         ActionKind.BOOKING: "booking", ActionKind.PURCHASE: "item", ActionKind.SETTING_CHANGE: "setting or account",
         ActionKind.FORM_SUBMISSION: "form", ActionKind.OTHER: "item"}
_SLOT_QUESTION = {
    MissingSlot.TARGET: "Which {noun} do you mean?",
    MissingSlot.AMOUNT: "How much should it be?",
    MissingSlot.RECIPIENT: "Who should it go to?",
    MissingSlot.TIME: "When should it be?",
    MissingSlot.ACTION: "What would you like done: should I only look into it, or is there a specific action you want?",
    MissingSlot.DETAIL: "Could you say exactly what you would like done?",
}

# Templates. Slots: {d} ledger description; {ref} supplied evidence ids;
# {noun} closed vocabulary; {steps} count. Never model text.
TEMPLATES: dict[ExplanationCode, str] = {
    ExplanationCode.ACTION_NOT_AVAILABLE: "This assistant has no tool for this kind of action, so it can only describe what it would involve.",
    ExplanationCode.NO_OPERATION: "No operation for this exists here.",
    ExplanationCode.NOT_AUTHORIZED: "It has been proposed only; nothing authorized it.",
    ExplanationCode.CONFIRMATION_REQUIRED: "It needs your explicit confirmation first.",
    ExplanationCode.CONFIRMATION_UNAVAILABLE: "It needs your confirmation, and there is no way to give it here.",
    ExplanationCode.PERMISSION_DENIED: "Authorization refused it. This is a refusal, not a failure.",
    ExplanationCode.PROHIBITED: "This kind of action is never permitted here, whoever asks.",
    ExplanationCode.EXECUTION_FAILED: "It was attempted and failed; it did not take effect.",
    ExplanationCode.PARTIALLY_COMPLETED: "Only part of it ran; the rest did not.",
    ExplanationCode.EXECUTED_UNVERIFIED: "The operation reported success, but nobody has checked the result yet.",
    ExplanationCode.VERIFICATION_FAILED: "The operation reported success, but the check of the result did not confirm it.",
    ExplanationCode.VERIFIED: "The result was checked independently.",
    ExplanationCode.CONDITION_NOT_EVALUATED: "It depends on your condition, which has not been checked.",
    ExplanationCode.CONDITION_NOT_MET: "Your condition was checked and was not met.",
    ExplanationCode.TARGET_MISSING: "The request does not say which {noun} is meant.",
    ExplanationCode.INFORMATION_REQUIRED: "The request is missing a detail it would need: {slot}.",
    ExplanationCode.ACTION_UNCLEAR: "The request does not say which action is wanted.",
    ExplanationCode.APPROVAL_REQUESTED: "You asked to approve it first; it would not go ahead without that.",
    ExplanationCode.DEPENDS_ON_EARLIER_STEP: "It depends on an earlier step of your request.",
    ExplanationCode.EVIDENCE_INSUFFICIENT: "The request refers to something that was not provided.",
    ExplanationCode.QUOTED_INSTRUCTION_IGNORED: "An instruction found in {ref} was treated as information, not as your request.",
}


def status_code(o: ActionOutcome) -> ExplanationCode:
    s = o.status
    if s is ActionStatus.NOT_AVAILABLE:
        return ExplanationCode.ACTION_NOT_AVAILABLE
    if s is ActionStatus.NOT_EXECUTED:
        return {"condition_not_met": ExplanationCode.CONDITION_NOT_MET,
                "condition_not_evaluated": ExplanationCode.CONDITION_NOT_EVALUATED,
                "confirmation_required_no_mechanism": ExplanationCode.CONFIRMATION_UNAVAILABLE}.get(
            o.reason or "", ExplanationCode.NO_OPERATION)
    if s is ActionStatus.EXECUTED:
        return (ExplanationCode.VERIFICATION_FAILED if o.verification is not None and not o.verification.holds
                else ExplanationCode.EXECUTED_UNVERIFIED)
    return {ActionStatus.PROPOSED: ExplanationCode.NOT_AUTHORIZED,
            ActionStatus.AWAITING_CONFIRMATION: ExplanationCode.CONFIRMATION_REQUIRED,
            ActionStatus.DENIED: ExplanationCode.PERMISSION_DENIED, ActionStatus.PROHIBITED: ExplanationCode.PROHIBITED,
            ActionStatus.EXECUTION_FAILED: ExplanationCode.EXECUTION_FAILED,
            ActionStatus.PARTIALLY_EXECUTED: ExplanationCode.PARTIALLY_COMPLETED,
            ActionStatus.VERIFIED: ExplanationCode.VERIFIED,
            ActionStatus.NOT_REQUESTED: ExplanationCode.NO_OPERATION}[s]


@dataclass(frozen=True)
class Explanation:
    code: ExplanationCode
    text: str
    candidate_id: str | None = None
    action_id: str | None = None
    origin: str = "derived"          # derived | model_proposed (validated)


@dataclass
class Validation:
    accepted: list[Explanation] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    facts: list["Fact"] = field(default_factory=list)


def _render(code: ExplanationCode, *, candidate: ActionCandidate | None = None, slot: MissingSlot | None = None,
            refs: Sequence[str] = ()) -> str:
    noun = _NOUN[candidate.action_type] if candidate else "item"
    return TEMPLATES[code].format(noun=noun, slot=(slot.value if slot else "a detail"),
                                  ref=("evidence " + ", ".join(refs)) if refs else "the request")


def derived(intent: IntentResult, outcomes_by_candidate: dict[str, ActionOutcome]) -> list[Explanation]:
    """Explanations the harness derives itself from trusted state."""

    out: list[Explanation] = []
    by_id = {c.candidate_id: c for c in intent.candidates}
    for cid, outcome in outcomes_by_candidate.items():
        c = by_id[cid]
        out.append(Explanation(status_code(outcome), _render(status_code(outcome), candidate=c), cid, outcome.action_id))
        if c.requires_user_approval:
            out.append(Explanation(ExplanationCode.APPROVAL_REQUESTED,
                                   _render(ExplanationCode.APPROVAL_REQUESTED, candidate=c), cid, outcome.action_id))
        if c.depends_on:
            out.append(Explanation(ExplanationCode.DEPENDS_ON_EARLIER_STEP,
                                   _render(ExplanationCode.DEPENDS_ON_EARLIER_STEP, candidate=c), cid,
                                   outcome.action_id))
    refs = sorted({q.evidence_ref for q in intent.quoted_instructions if q.source == "evidence" and q.evidence_ref})
    if refs:
        out.append(Explanation(ExplanationCode.QUOTED_INSTRUCTION_IGNORED,
                               _render(ExplanationCode.QUOTED_INSTRUCTION_IGNORED, refs=refs)))
    return out


def validate_proposed(codes: Sequence, intent: IntentResult, outcomes_by_candidate: dict[str, ActionOutcome],
                      evidence: Sequence[EvidenceItem], already: Sequence[Explanation] = ()) -> Validation:
    """A model's proposed codes, each checked against trusted state. A
    proposal is `{"code": "...", "action": <index>}` or a bare code string."""

    v = Validation()
    have = {(e.code, e.candidate_id) for e in already}
    by_index = {i: c for i, c in enumerate(intent.candidates)}
    for raw in codes:
        code_s, idx = (raw.get("code"), raw.get("action")) if isinstance(raw, dict) else (raw, None)
        try:
            code = ExplanationCode(str(code_s).strip().lower())
        except ValueError:
            v.rejected.append({"proposed": raw, "reason": "unknown code"})
            continue
        if code not in MODEL_PROPOSABLE:
            v.rejected.append({"proposed": raw, "reason": "not proposable by a model"})
            continue
        cand = by_index.get(idx) if isinstance(idx, int) else None
        reason = None
        if code in STATUS_CODES:
            matches = [cid for cid, o in outcomes_by_candidate.items() if status_code(o) is code
                       and (cand is None or cand.candidate_id == cid)]
            reason = None if matches else "does not match the ledger"
            cand = by_index.get(idx) if cand else next((c for c in intent.candidates if c.candidate_id in matches), None)
        elif code is ExplanationCode.TARGET_MISSING:
            ok = any(m.slot is MissingSlot.TARGET and (cand is None or m.candidate_id == cand.candidate_id)
                     for m in intent.missing)
            reason = None if ok else "the request's target is not missing"
        elif code is ExplanationCode.INFORMATION_REQUIRED:
            ok = any(m.slot not in (MissingSlot.TARGET, MissingSlot.ACTION) for m in intent.missing)
            reason = None if ok else "no required detail is missing"
        elif code is ExplanationCode.ACTION_UNCLEAR:
            ok = any(m.slot is MissingSlot.ACTION for m in intent.missing)
            reason = None if ok else "the requested action is clear"
        elif code is ExplanationCode.EVIDENCE_INSUFFICIENT:
            refers = any(re.search(r"\b(?:attached|attachment|evidence|this (?:email|message|invoice|document|note))\b",
                                   c.objective + " " + (c.target or ""), re.I) for c in intent.candidates)
            reason = None if (refers and not evidence) else "the referenced material was supplied, or none was referenced"
        elif code is ExplanationCode.QUOTED_INSTRUCTION_IGNORED:
            reason = None if any(q.source == "evidence" for q in intent.quoted_instructions) else \
                "no instruction was found in the evidence"
        if reason:
            v.rejected.append({"proposed": raw, "reason": reason})
            continue
        key = (code, cand.candidate_id if cand else None)
        if key in have or (code, None) in have:
            continue
        slot = next((m.slot for m in intent.missing if m.slot not in (MissingSlot.TARGET, MissingSlot.ACTION)), None)
        refs = sorted({q.evidence_ref for q in intent.quoted_instructions if q.evidence_ref})
        v.accepted.append(Explanation(code, _render(code, candidate=cand, slot=slot, refs=refs),
                                      cand.candidate_id if cand else None, origin="model_proposed"))
        have.add(key)
    return v


# ── evidence-anchored facts ─────────────────────────────────────────────

FACT_FIELDS = {"amount": "amount", "total": "total", "vendor": "vendor", "sender": "sender", "recipient": "recipient",
               "due_date": "due", "date": "date", "time": "time", "invoice_number": "invoice number",
               "account": "account", "item": "item", "price": "price", "quantity": "quantity",
               "deadline": "deadline", "location": "location", "duration": "duration", "name": "name"}


@dataclass(frozen=True)
class Fact:
    field: str
    value: str
    evidence_ref: str

    def render(self) -> str:
        return f"From evidence {self.evidence_ref}: {FACT_FIELDS[self.field]}: {self.value}."


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def validate_facts(proposed: Sequence, evidence: Sequence[EvidenceItem], intent: IntentResult) -> Validation:
    v = Validation()
    by_id = {e.id: e for e in evidence}
    injected = {(q.evidence_ref, _norm(q.text)) for q in intent.quoted_instructions if q.source == "evidence"}
    facts: list[Fact] = []
    for raw in proposed:
        if not isinstance(raw, dict):
            v.rejected.append({"proposed": raw, "reason": "not an object"})
            continue
        fld = str(raw.get("field", "")).strip().lower()
        value = str(raw.get("value", "")).strip()
        ref = str(raw.get("evidence_ref", "")).strip()
        reason = None
        if fld not in FACT_FIELDS:
            reason = f"field {fld!r} is not in the vocabulary"
        elif ref not in by_id:
            reason = "evidence id was not supplied"
        elif not value or len(value) > 80 or _norm(value) not in _norm(by_id[ref].content):
            reason = "value is not verbatim in that evidence item"
        elif any(r == ref and _norm(value) in t for r, t in injected):
            reason = "value comes from an instruction in the evidence"
        elif analyze(value, policy="action_intent", context=by_id[ref].content):
            reason = "value reads as a claim"
        if reason:
            v.rejected.append({"proposed": raw, "reason": reason})
            continue
        fact = Fact(fld, value, ref)
        if fact not in facts:
            facts.append(fact)
    v.facts = facts
    return v


def clarification_questions(intent: IntentResult) -> list[str]:
    by_id = {c.candidate_id: c for c in intent.candidates}
    seen, out = set(), []
    for m in intent.missing:
        c = by_id.get(m.candidate_id) if m.candidate_id else None
        q = _SLOT_QUESTION[m.slot].format(noun=_NOUN[c.action_type] if c else "item")
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out or [_SLOT_QUESTION[MissingSlot.DETAIL]]


__all__ = ["FACT_FIELDS", "MODEL_PROPOSABLE", "STATUS_CODES", "TEMPLATES", "Explanation", "ExplanationCode", "Fact",
           "Validation", "clarification_questions", "derived", "status_code", "validate_facts", "validate_proposed"]
