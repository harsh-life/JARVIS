"""The response-truthfulness boundary (Phase 0.1, docs/31 §9). Experiment-local.

What the user sees about an action is decided here, from the harness's own
`ActionLedger`, and nowhere else.

**Action-intent tasks** (the ledger holds at least one requested action): the
user-facing response is built **entirely** from the ledger: one fixed
sentence per action (`status_line`), and nothing else. No model-written prose
is rendered, whatever it says and whatever the screen thinks of it. The
model's explanation is kept in the finalization record
(`explanation_withheld`, each sentence with what the screen found), for
debugging and evaluation only. So for these tasks truthfulness does not depend
on detecting a false claim: a paraphrase that no detector would catch still
never reaches the user. A completion statement appears only if the ledger
holds a successful execution record, and a goal-achievement statement only if
it also holds a verification whose postcondition held.

**Informational tasks** (no action requested): the model's answer is the
response, screened sentence by sentence by `claims.py`. A sentence asserting a
side effect, the assistant's own act, or a receipt is dropped. If nothing
survives, a fixed fallback is shown. A reply that invents execution-state
fields (`execution_id`, `receipt`, …) is withheld entirely. This screen is
lexical and incomplete, so it is defence in depth for prose, **not** a
guarantee: what it misses reaches the user.

`verify` re-derives every harness sentence from the ledger and checks that no
model text is rendered on an action-intent task. On a mismatch it drops model
text; it never adds anything.

Version 2: action-intent responses are status-only (v1 appended model
sentences the screen admitted). `awaiting_confirmation` no longer names a
confirmation UI.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal, Sequence

from experiments.actions import ActionLedger, ActionOutcome, ActionStatus
from experiments.claims import Claim, Policy, analyze, sentences

FINALIZER_VERSION = "2"

# JSON keys through which a model would be asserting execution state that only
# the harness can know. Their presence alone is a fabrication.
STATE_KEYS = frozenset({
    "status", "action_status", "execution", "execution_id", "execution_ids", "executed", "performed",
    "performed_actions", "completed_actions", "actions_taken", "actions_performed", "result", "result_id",
    "transaction_id", "confirmation", "confirmation_number", "confirmation_id", "receipt", "receipt_id",
    "reference_number", "verification", "verification_id", "verified", "done",
})

INFORMATIONAL_FALLBACK = "I can't give an answer to this that I can stand behind."


def status_line(o: ActionOutcome) -> str:
    """The only sentences that ever state an action's status to the user.
    None of them names a confirmation UI, an app or any mechanism the harness
    does not have."""

    joiner = "only after" if o.condition_kind in ("temporal", "user_step") else "only if"
    d = o.description if o.condition is None else f"{o.description}, {joiner} “{o.condition}”"
    s = o.status
    if s is ActionStatus.NOT_AVAILABLE:
        return f"Not done: {d}. There is no tool or permission for this here, so nothing was carried out."
    if s is ActionStatus.NOT_EXECUTED and o.reason == "condition_not_met":
        return f"Not done: {d}. Your condition was checked and was not met, so nothing was carried out."
    if s is ActionStatus.NOT_EXECUTED and o.reason == "condition_not_evaluated":
        return f"Not done: {d}. Your condition has not been checked, so nothing was carried out."
    if s is ActionStatus.NOT_EXECUTED and o.reason == "confirmation_required_no_mechanism":
        return f"Not done: {d}. It would need your confirmation, and there is no way to give it here, so nothing was carried out."
    if s is ActionStatus.NOT_EXECUTED or s is ActionStatus.NOT_REQUESTED:
        return f"Not done: {d}. No operation for this was run, so nothing was carried out."
    if s is ActionStatus.PROPOSED:
        return f"Not done: {d}. It was proposed but has not been authorized, and nothing has been carried out."
    if s is ActionStatus.AWAITING_CONFIRMATION:
        return f"Not done yet: {d}. It needs your confirmation before anything can happen, and nothing has been carried out."
    if s is ActionStatus.DENIED:
        return f"Not done: {d}. It was refused when authorization was checked, and nothing was carried out."
    if s is ActionStatus.PROHIBITED:
        return f"Not done: {d}. This kind of action is not permitted here, and nothing was carried out."
    assert o.execution is not None
    e = o.execution
    if s is ActionStatus.EXECUTION_FAILED:
        return (f"Failed: {d}. It was attempted and the operation reported failure (execution {e.execution_id}), "
                "so it did not take effect.")
    if s is ActionStatus.PARTIALLY_EXECUTED:
        return (f"Partly done: {d}. {e.steps_completed} of {e.steps_total} steps completed before it stopped "
                f"(execution {e.execution_id}); the remaining steps were not carried out, and the result has not "
                "been checked.")
    if s is ActionStatus.EXECUTED:
        v = o.verification
        if v is not None and not v.holds:
            return (f"Carried out, but not confirmed: {d}. The operation reported success (execution "
                    f"{e.execution_id}), but an independent check did not confirm that {v.postcondition} "
                    f"(check {v.verification_id}).")
        return (f"Carried out, not yet verified: {d}. The operation reported success (execution {e.execution_id}), "
                "but the result has not been independently checked.")
    assert s is ActionStatus.VERIFIED and o.verification is not None
    v = o.verification
    return (f"Done and verified: {d}. The operation reported success (execution {e.execution_id}), and an "
            f"independent check confirmed that {v.postcondition} (check {v.verification_id}).")


@dataclass(frozen=True)
class Segment:
    text: str
    source: Literal["harness", "model"]
    action_id: str | None = None


@dataclass
class Finalized:
    text: str
    policy: Policy
    task_action_status: str
    segments: list[Segment] = field(default_factory=list)
    # Informational tasks: sentences dropped by the screen.
    suppressed: list[dict] = field(default_factory=list)
    # Action-intent tasks: the model's explanation, never rendered. Each
    # sentence carries what the screen found in it (for measuring the screen
    # and the cost of withholding), not a decision.
    explanation_withheld: list[dict] = field(default_factory=list)
    withheld_all: bool = False
    withheld_reason: str | None = None
    asserted_state_keys: list[str] = field(default_factory=list)
    outcomes: list[dict] = field(default_factory=list)
    version: str = FINALIZER_VERSION

    @property
    def model_text(self) -> str:
        return " ".join(s.text for s in self.segments if s.source == "model")

    @property
    def explanation_contradicted_record(self) -> bool:
        return any(e["claims"] for e in self.explanation_withheld)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["segments"] = [asdict(s) for s in self.segments]
        d["explanation_contradicted_record"] = self.explanation_contradicted_record
        return d


class FinalizerInvariant(AssertionError):
    pass


def _claim_dicts(claims: Sequence[Claim]) -> list[dict]:
    return [{"kind": c.kind, "rule": c.rule, "span": c.span} for c in claims]


def finalize(model_text: str | None, ledger: ActionLedger, *, context: str = "", parsed: bool = True,
             model_fields: Sequence[str] = ()) -> Finalized:
    """→ the user-visible response. `model_text` is what the model wrote for
    the user; `context` is the text the harness supplied (prompt, evidence),
    against which identifiers in model text are checked; `model_fields` are the
    top-level keys of the model's JSON reply."""

    outcomes = ledger.outcomes()
    policy: Policy = "action_intent" if outcomes else "informational"
    issued = ledger.issued()
    asserted = sorted(k for k in model_fields if k.lower() in STATE_KEYS)
    result = Finalized(text="", policy=policy, task_action_status=ledger.task_status().value,
                       asserted_state_keys=asserted, outcomes=[_outcome_dict(o) for o in outcomes])
    full_context = context + "\n" + "\n".join(o.description for o in outcomes)
    screened = [(s, analyze(s, policy=policy, context=full_context, issued_ids=issued))
                for s in sentences(model_text or "")]

    if policy == "action_intent":
        # The response is the ledger's, entirely. Nothing the model wrote is shown.
        result.explanation_withheld = [{"sentence": s, "claims": _claim_dicts(c)} for s, c in screened]
        if screened:
            result.withheld_all, result.withheld_reason = True, "action_status_is_deterministic"
        result.segments = [Segment(status_line(o), "harness", o.action_id) for o in outcomes]
        result.text = _join(result.segments)
        verify(result, ledger, context=full_context)
        return result

    admitted: list[str] = []
    for sentence, claims in screened:
        if claims:
            result.suppressed.append({"sentence": sentence, "claims": _claim_dicts(claims)})
        else:
            admitted.append(sentence)
    if asserted:
        result.suppressed.extend({"sentence": s, "claims": [], "withheld_with_reply": True} for s in admitted)
        admitted = []
        result.withheld_all, result.withheld_reason = True, "model_asserted_execution_state"
    segments = [Segment(s, "model") for s in admitted]
    if not admitted and (result.suppressed or result.withheld_all):
        # Everything was withheld: say so, rather than show an empty reply that
        # would read as an answer.
        segments = [Segment(INFORMATIONAL_FALLBACK, "harness")]
    result.segments = segments
    result.text = _join(segments)
    try:
        verify(result, ledger, context=full_context)
    except FinalizerInvariant as exc:
        result.suppressed.extend({"sentence": s.text, "claims": [], "invariant": str(exc)}
                                 for s in result.segments if s.source == "model")
        result.segments = [Segment(INFORMATIONAL_FALLBACK, "harness")]
        result.withheld_all, result.withheld_reason = True, f"invariant: {exc}"
        result.text = _join(result.segments)
    return result


def verify(final: Finalized, ledger: ActionLedger, *, context: str = "") -> None:
    """The finalizer's postconditions, checked on its own output."""

    by_id = {o.action_id: o for o in ledger.outcomes()}
    issued = ledger.issued()
    rendered = [s.action_id for s in final.segments if s.source == "harness" and s.action_id]
    if sorted(rendered) != sorted(by_id):
        raise FinalizerInvariant("every requested action must have exactly one status line")
    if by_id and any(s.source == "model" for s in final.segments):
        raise FinalizerInvariant("an action-intent response renders no model text")
    for seg in final.segments:
        if seg.source == "harness":
            if seg.action_id is None:
                if by_id or seg.text != INFORMATIONAL_FALLBACK:
                    raise FinalizerInvariant("unknown harness sentence")
                continue
            o = by_id[seg.action_id]
            if seg.text != status_line(o):
                raise FinalizerInvariant(f"status line for {seg.action_id} does not match the ledger")
            if seg.text.startswith(("Carried out", "Done and verified")) and o.execution is None:
                raise FinalizerInvariant("execution stated without an execution record")
            if seg.text.startswith("Done and verified") and not (o.verification and o.verification.holds):
                raise FinalizerInvariant("goal stated without a holding verification")
            continue
        if any(i in seg.text for i in issued):
            raise FinalizerInvariant("model text may not cite a ledger id")
        if analyze(seg.text, policy=final.policy, context=context, issued_ids=issued):
            raise FinalizerInvariant("model text asserts a side effect")


def _join(segments: Sequence[Segment]) -> str:
    status = [s.text for s in segments if s.source == "harness" and s.action_id]
    other = [s.text for s in segments if not (s.source == "harness" and s.action_id)]
    parts = []
    if status:
        parts.append("\n".join(status))
    if other:
        parts.append(" ".join(other))
    return "\n\n".join(parts)


def _outcome_dict(o: ActionOutcome) -> dict:
    return {"action_id": o.action_id, "kind": o.kind.value, "description": o.description,
            "status": o.status.value, "history": [h.value for h in o.history], "reason": o.reason,
            "proposed_by_model": o.proposed_by_model,
            "confirmation_ref": o.confirmation.confirmation_id if o.confirmation else None,
            "execution_ref": o.execution_ref,
            "execution_result": o.execution.result if o.execution else None,
            "verification_ref": o.verification_ref,
            "verification_holds": o.verification.holds if o.verification else None}


__all__ = ["FINALIZER_VERSION", "INFORMATIONAL_FALLBACK", "STATE_KEYS", "Finalized", "FinalizerInvariant",
           "Segment", "finalize", "status_line", "verify"]
