"""The Judge's pure contract (19 §4, §5, §8, §9): trace safety, strict output
parsing, metering by construction, and the closed improvement-target registry.

No runtime, no database: these are the properties that must hold *before* the
Judge is wired to anything.
"""

from __future__ import annotations

import uuid

import pytest

from server.evaluation.candidates import (
    ALLOWED_TARGETS,
    CandidateRejected,
    validate_candidate,
    validate_value,
)
from server.evaluation.metering import (
    CURRENT_JUDGE_METER,
    JudgeOverBudget,
    MeteredJudgeModel,
    RecordingMeter,
    UnmeteredJudgeCall,
)
from server.evaluation.provider import LLMJudge, MalformedEvaluation, RulesJudge, parse_judge_output
from server.evaluation.trace import MAX_STEPS, SourceEvent, SourceMessage, TaskTrace, TraceSource, build_trace
from server.models.provider import ChatMessage, ModelPricing, ModelResult, ModelSpec, ModelUnavailable
from server.security.secret_patterns import find_secret, redact_secrets
from shared.schemas.evaluation import Anomaly, EvaluationKind, ImprovementCandidate, JudgeVerdict

PLANTED = {
    "openai": "sk-proj-AbCdEfGhIjKlMnOpQrStUv123456",
    "aws": "AKIAABCDEFGHIJKLMNOP",
    "github": "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "pem": "-----BEGIN RSA PRIVATE KEY-----",
    "assignment": "password is correct-horse-battery",
    "handle": "secretstore:model-key-7",
    "entropy": "Zx9Qw7Er5Ty3Ui1OpAs8Df6Gh4Jk2Lz",
}


def source(*, user_id: uuid.UUID | None = None, messages=(), events=(), request="summarize my notes",
           response="here is the summary", **overrides) -> TraceSource:
    fields = dict(
        task_id=uuid.uuid4(), user_id=user_id or uuid.uuid4(), graph_id=None, mode="execute",
        user_request=request, status="completed", failure_code=None, final_response=response,
        unresolved=False, iterations=2, model_calls=2, tool_calls=1, worker_switches=0, denials=0,
        violations=0, rejections=0, tripped_source=None, elapsed_seconds=1.5,
        messages=tuple(messages), events=tuple(events),
    )
    fields.update(overrides)
    return TraceSource(**fields)


def trace_of(src: TraceSource, kind=EvaluationKind.POST_HOC, **kw) -> TaskTrace:
    return build_trace(src, kind=kind, max_chars=kw.pop("max_chars", 2000), **kw)


# ── JDG-T6: no secret value reaches a Judge ────────────────────────────────


@pytest.mark.parametrize("name, secret", sorted(PLANTED.items()))
def test_a_planted_secret_is_redacted_everywhere_in_the_trace(name, secret):
    src = source(
        request=f"use {secret} please",
        response=f"done with {secret}",
        messages=[SourceMessage("assistant", f'{{"type": "tool_call", "arguments": {{"k": "{secret}"}}}}'),
                  SourceMessage("user", f"OBSERVATION: the file says {secret}")],
        events=[SourceEvent(position=1, at=0.1, name="agent.tool.failed", result="failure",
                            resource=f"tool:x.y:{secret}")],
    )
    trace = trace_of(src)
    text = trace.to_json()
    assert secret not in text
    assert find_secret(text) is None
    assert trace.redactions, "the redaction is reported (by pattern name) so it can be audited"
    assert all(secret not in r for r in trace.redactions)


def test_redaction_leaves_ordinary_text_alone():
    text = "list the files in reports/2026 and count the words in notes.txt"
    assert redact_secrets(text) == (text, [])


# ── JDG-T7: one task, one user ─────────────────────────────────────────────


def test_a_trace_is_built_from_exactly_one_source():
    a, b = source(), source()
    with pytest.raises(TypeError):
        build_trace([a, b], kind=EvaluationKind.POST_HOC, max_chars=100)  # type: ignore[arg-type]
    trace = trace_of(a)
    assert trace.user_id == a.user_id and trace.task_id == a.task_id


def test_the_judge_never_sees_who_the_user_is():
    src = source()
    payload = trace_of(src).for_judge()
    assert str(src.user_id) not in trace_of(src).to_json()
    assert "user_id" not in payload and "graph_id" not in payload


async def test_the_llm_judge_refuses_anything_but_one_trace():
    judge = LLMJudge(_Scripted("{}"))
    with pytest.raises(TypeError):
        await judge.evaluate([trace_of(source()), trace_of(source())], EvaluationKind.POST_HOC)  # type: ignore[arg-type]


# ── bounds and windows ─────────────────────────────────────────────────────


def test_observations_are_bounded_and_windows_keep_stable_refs():
    msgs = [SourceMessage("user", "x" * 5000) for _ in range(30)]
    trace = trace_of(source(messages=msgs), max_chars=200)
    assert all(len(s.text) <= 200 + len(" [truncated]") for s in trace.steps)
    window = trace_of(source(messages=msgs), kind=EvaluationKind.LIVE_WINDOW, window_steps=5)
    assert [s.ref for s in window.steps] == ["s26", "s27", "s28", "s29", "s30"]
    assert window.truncated and window.total_steps == 30
    many = trace_of(source(messages=[SourceMessage("user", "y")] * (MAX_STEPS + 10)))
    assert len(many.steps) == MAX_STEPS


def test_events_interleave_with_the_transcript_in_order():
    src = source(
        messages=[SourceMessage("assistant", "p1"), SourceMessage("user", "o1"), SourceMessage("assistant", "p2")],
        events=[SourceEvent(position=1, at=0.2, name="authz", decision="allow", risk="low_read"),
                SourceEvent(position=2, at=0.3, name="agent.tool.executed", result="success")],
    )
    kinds = [(s.kind, s.text or s.name) for s in trace_of(src).steps]
    assert kinds == [("proposal", "p1"), ("event", "authz"), ("observation", "o1"),
                     ("event", "agent.tool.executed"), ("proposal", "p2")]


# ── 19 §5: malformed output is rejected, never coerced (JDG-T4/T5) ─────────


@pytest.mark.parametrize("output, code", [
    ("", "empty_output"),
    ("The task looks fine.", "not_a_single_json_object"),
    ('Sure! {"quality": 0.9}', "not_a_single_json_object"),
    ('{"quality": 0.9} thanks', "not_a_single_json_object"),
    ("[0.9]", "not_a_single_json_object"),
    ('{"quality": 0.9}{"quality": 0.1}', "invalid_json"),
    ('{"quality": 1.5}', "schema_violation"),
    ('{"quality": NaN}', "schema_violation"),
    ('{"efficiency": -0.1}', "schema_violation"),
    ('{"reward": {"credit": 7}}', "schema_violation"),
    ('{"anomaly": "stop_requested"}', "schema_violation"),
    ('{"anomaly": "stop_requested", "anomaly_reason": "Drop the table!"}', "schema_violation"),
    ('{"anomaly": "kill"}', "schema_violation"),
    ('{"redundant_steps": ["s999"]}', "unknown_step_ref"),
    # Judge "authority" responses — every one is an unknown key.
    ('{"quality": 1, "approve": true}', "schema_violation"),
    ('{"quality": 1, "approved": true}', "schema_violation"),
    ('{"quality": 1, "confirm": true}', "schema_violation"),
    ('{"quality": 1, "confirmation_token": "abc"}', "schema_violation"),
    ('{"quality": 1, "resume": true}', "schema_violation"),
    ('{"quality": 1, "authorize": {"capability": "file.write"}}', "schema_violation"),
    ('{"quality": 1, "grant": "system.restricted"}', "schema_violation"),
    ('{"quality": 1, "risk_category": "low_read"}', "schema_violation"),
    ('{"quality": 1, "confinement_mode": "unconfined"}', "schema_violation"),
    ('{"quality": 1, "task_id": "00000000-0000-0000-0000-000000000000"}', "schema_violation"),
    ('{"quality": 1, "evaluator_id": "operator"}', "schema_violation"),
    ('{"quality": 1, "action": "execute", "tool": "files.write"}', "schema_violation"),
])
def test_malformed_or_authority_seeking_output_is_rejected(output, code):
    trace = trace_of(source(messages=[SourceMessage("assistant", "p")]))
    with pytest.raises(MalformedEvaluation) as caught:
        parse_judge_output(output, trace=trace, kind=EvaluationKind.POST_HOC,
                           evaluator_id="llm_judge", evaluator_version="1")
    assert caught.value.code == code


def test_valid_output_is_attributed_by_deterministic_code_not_by_the_judge():
    trace = trace_of(source(messages=[SourceMessage("assistant", "p"), SourceMessage("user", "o")]))
    evaluation = parse_judge_output(
        '```json\n{"quality": 0.8, "efficiency": 0.5, "redundant_steps": ["s1"], '
        '"failures": [{"step_ref": "s2", "category": "wrong_file", "note": "read the wrong file"}], '
        '"anomaly": "suspicious", "anomaly_reason": "odd_retries", '
        '"reward": {"credit": 0.25, "attributed_to": ["s1"]}}\n```',
        trace=trace, kind=EvaluationKind.POST_HOC, evaluator_id="llm_judge", evaluator_version="1",
    )
    assert evaluation.task_id == trace.task_id and evaluation.kind is EvaluationKind.POST_HOC
    assert evaluation.quality == 0.8 and evaluation.anomaly is Anomaly.SUSPICIOUS


def test_the_verdict_schema_has_no_field_that_could_carry_authority():
    fields = set(JudgeVerdict.model_fields)
    for forbidden in ("approve", "approved", "confirm", "confirmation", "grant", "authorize", "resume",
                      "tier", "risk_category", "capability", "execute", "tool", "confinement_mode"):
        assert forbidden not in fields


# ── 19 §8: every Judge model call is metered ───────────────────────────────


class _Scripted:
    def __init__(self, content: str, *, paid: bool = False, fail: BaseException | None = None) -> None:
        pricing = ModelPricing(1.0, 1.0) if paid else ModelPricing()
        self.spec = ModelSpec(provider="ollama" if not paid else "openai", model="judge", pricing=pricing,
                              timeout_seconds=5)
        self.content, self.fail, self.calls = content, fail, 0

    async def invoke(self, messages, *, timeout):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return ModelResult(content=self.content, prompt_tokens=100, completion_tokens=20)

    async def health(self):
        return True


async def test_a_judge_model_call_without_a_meter_is_refused():
    inner = _Scripted("{}")
    with pytest.raises(UnmeteredJudgeCall):
        await MeteredJudgeModel(inner).invoke([ChatMessage("user", "x")], timeout=1)
    assert inner.calls == 0


async def test_metered_calls_are_recorded_on_success_and_failure():
    async def allow(_projected):
        return None

    meter = RecordingMeter(budget_check=allow)
    token = CURRENT_JUDGE_METER.set(meter)
    try:
        await MeteredJudgeModel(_Scripted("{}", paid=True)).invoke([ChatMessage("user", "x")], timeout=1)
        with pytest.raises(ModelUnavailable):
            await MeteredJudgeModel(_Scripted("{}", fail=ModelUnavailable("down"))).invoke(
                [ChatMessage("user", "x")], timeout=1)
    finally:
        CURRENT_JUDGE_METER.reset(token)
    assert [r.units for r in meter.records] == [120, 0]
    assert meter.records[0].cost == pytest.approx(0.12)


async def test_an_over_budget_judge_call_is_never_made():
    async def refuse(_projected):
        raise JudgeOverBudget("evaluation_budget")

    inner = _Scripted("{}", paid=True)
    token = CURRENT_JUDGE_METER.set(RecordingMeter(budget_check=refuse))
    try:
        with pytest.raises(JudgeOverBudget):
            await MeteredJudgeModel(inner).invoke([ChatMessage("user", "x")], timeout=1)
    finally:
        CURRENT_JUDGE_METER.reset(token)
    assert inner.calls == 0


# ── the rules evaluator ────────────────────────────────────────────────────


async def test_the_rules_judge_records_but_never_requests_a_stop():
    src = source(
        messages=[SourceMessage("assistant", "same"), SourceMessage("assistant", "same")],
        events=[SourceEvent(position=1, at=0.1, name="agent.tool.failed", result="failure")],
        violations=3, denials=5,
    )
    evaluation = await RulesJudge().evaluate(trace_of(src), EvaluationKind.LIVE_WINDOW)
    assert evaluation.anomaly is Anomaly.SUSPICIOUS
    assert evaluation.redundant_steps == ["s3"]
    assert evaluation.failures and evaluation.efficiency == 0.5


# ── JDG-T9: the closed improvement-target registry ─────────────────────────


FORBIDDEN = [
    "capability.registry", "capabilities", "operation.mapping", "tier.table", "tiers", "risk.file.write",
    "floor.names", "authorization.rules", "authz", "grants.default", "confirmation.policy", "graph.roles",
    "role.owner", "visibility.predicate", "confinement.mode", "sandbox.root", "egress.policy",
    "network.destinations", "execution.process.confinement_mode", "break_glass.enabled", "secret.value",
    "secrets.kek", "secretstore.handles", "breaker.denial_limit", "breaker.one_way", "judge.permissions",
    "evaluation.may_request_stop", "evaluation.enabled", "evaluation.approve_candidates", "security.budgets",
    "superuser.token", "audit.retention", "usage.limits", "budget.per_task",
]


@pytest.mark.parametrize("target", FORBIDDEN)
def test_a_candidate_aimed_at_security_policy_is_rejected_at_creation(target):
    candidate = ImprovementCandidate(target=target, proposed_change="1", evidence=[])
    with pytest.raises(CandidateRejected) as caught:
        validate_candidate(candidate, task_id=str(uuid.uuid4()))
    assert caught.value.code == "forbidden_target"


@pytest.mark.parametrize("target", ["made.up", "worker", "worker.model", "recovery.chain", "agent.primary"])
def test_anything_outside_the_allow_list_is_rejected(target):
    with pytest.raises(CandidateRejected):
        validate_candidate(ImprovementCandidate(target=target, proposed_change="x"), task_id="t")


@pytest.mark.parametrize("target, value, expected", [
    ("worker.system_prompt", "List a directory before reading from it.", "List a directory before reading from it."),
    ("worker.tool_description:files.read", "Reads one file.", "Reads one file."),
    ("recovery.stall_window", "4", 4),
    ("recovery.loop_repeat_limit", "2", 2),
    ("recovery.max_worker_switches", "0", 0),
    ("evaluation.rubric", "Penalise redundant reads.", "Penalise redundant reads."),
    ("suggestion.template", "Consider: {task}", "Consider: {task}"),
])
def test_allowed_targets_are_accepted_with_their_typed_value(target, value, expected):
    task = str(uuid.uuid4())
    accepted = validate_candidate(ImprovementCandidate(target=target, proposed_change=value), task_id=task)
    assert accepted.value == expected and accepted.evidence == (task,)


@pytest.mark.parametrize("target, value, code", [
    ("recovery.stall_window", "0", "value_out_of_range"),
    ("recovery.stall_window", "11", "value_out_of_range"),
    ("recovery.max_worker_switches", "-1", "bad_value"),
    ("recovery.loop_repeat_limit", "two", "bad_value"),
    ("worker.system_prompt", "x" * 1501, "bad_value"),
    ("worker.system_prompt", "Use api_key = sk-proj-AbCdEfGhIjKlMnOpQrSt", "secret_in_value"),
    ("worker.tool_description", "no subject", "bad_subject"),
    ("worker.system_prompt:extra", "subject where none belongs", "bad_subject"),
])
def test_values_are_typed_bounded_and_secret_free(target, value, code):
    with pytest.raises(CandidateRejected) as caught:
        validate_candidate(ImprovementCandidate(target=target, proposed_change=value), task_id="t")
    assert caught.value.code == code


def test_a_candidate_may_cite_only_the_evaluated_task():
    mine, other = str(uuid.uuid4()), uuid.uuid4()
    with pytest.raises(CandidateRejected) as caught:
        validate_candidate(ImprovementCandidate(target="evaluation.rubric", proposed_change="x",
                                                evidence=[uuid.UUID(mine), other]), task_id=mine)
    assert caught.value.code == "foreign_evidence"


def test_the_registry_is_exactly_19_s9s_list():
    assert set(ALLOWED_TARGETS) == {
        "worker.system_prompt", "worker.tool_description", "recovery.stall_window",
        "recovery.loop_repeat_limit", "recovery.max_worker_switches", "evaluation.rubric",
        "suggestion.template",
    }
    with pytest.raises(CandidateRejected):
        validate_value("breaker.denial_limit", None, "100")

