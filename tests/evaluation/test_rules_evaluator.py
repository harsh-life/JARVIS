"""The rules evaluator's pure contract: trace safety, deterministic findings,
bounded output, and no authority-carrying field.

No runtime, no database. These properties must hold before the evaluator is
wired to anything. (Formerly part of the LLM Judge's contract suite; the Judge
and its parsing, metering and candidate registry are retired — D-JDG-RETIRE.)
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from server.evaluation.rules import RulesEvaluator
from server.evaluation.service import EvaluationService, EvaluationSettings
from server.evaluation.trace import MAX_STEPS, SourceEvent, SourceMessage, TaskTrace, TraceSource, build_trace
from server.security.secret_patterns import find_secret, redact_secrets
from shared.schemas.evaluation import (
    MAX_FAILURES,
    MAX_REDUNDANT_STEPS,
    Anomaly,
    EvaluationKind,
    EvaluationOutcome,
    RulesEvaluation,
)

PLANTED = {
    "openai": "sk-proj-AbCdEfGhIjKlMnOpQrStUv123456",  # TEST-ONLY fixture
    "aws": "AKIAABCDEFGHIJKLMNOP",  # TEST-ONLY fixture
    "github": "ghp_abcdefghijklmnopqrstuvwxyz0123456789",  # TEST-ONLY fixture
    "pem": "-----BEGIN RSA PRIVATE KEY-----",  # TEST-ONLY fixture
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


def trace_of(src: TraceSource, **kw) -> TaskTrace:
    return build_trace(src, kind=EvaluationKind.POST_HOC, max_chars=kw.pop("max_chars", 2000), **kw)


def _all_text(trace: TaskTrace) -> str:
    return json.dumps({
        "request": trace.user_request, "response": trace.final_response,
        "steps": [dataclasses.asdict(s) for s in trace.steps],
    }, default=str)


# ── the trace: no secret, one user, bounded ────────────────────────────────


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
    text = _all_text(trace)
    assert secret not in text
    assert find_secret(text) is None
    assert trace.redactions, "the redaction is reported (by pattern name) so it can be audited"
    assert all(secret not in r for r in trace.redactions)


def test_redaction_leaves_ordinary_text_alone():
    text = "list the files in reports/2026 and count the words in notes.txt"
    assert redact_secrets(text) == (text, [])


def test_a_trace_is_built_from_exactly_one_source():
    a, b = source(), source()
    with pytest.raises(TypeError):
        build_trace([a, b], kind=EvaluationKind.POST_HOC, max_chars=100)  # type: ignore[arg-type]
    trace = trace_of(a)
    assert trace.user_id == a.user_id and trace.task_id == a.task_id


def test_observations_are_bounded():
    msgs = [SourceMessage("user", "x" * 5000) for _ in range(30)]
    trace = trace_of(source(messages=msgs), max_chars=200)
    assert all(len(s.text) <= 200 + len(" [truncated]") for s in trace.steps)
    many = trace_of(source(messages=[SourceMessage("user", "y")] * (MAX_STEPS + 10)))
    assert len(many.steps) == MAX_STEPS and many.truncated


def test_events_interleave_with_the_transcript_in_order():
    src = source(
        messages=[SourceMessage("assistant", "p1"), SourceMessage("user", "o1"), SourceMessage("assistant", "p2")],
        events=[SourceEvent(position=1, at=0.2, name="authz", decision="allow", risk="low_read"),
                SourceEvent(position=2, at=0.3, name="agent.tool.executed", result="success")],
    )
    kinds = [(s.kind, s.text or s.name) for s in trace_of(src).steps]
    assert kinds == [("proposal", "p1"), ("event", "authz"), ("observation", "o1"),
                     ("event", "agent.tool.executed"), ("proposal", "p2")]


# ── the rules ──────────────────────────────────────────────────────────────


def test_redundant_proposals_and_failures_are_found():
    src = source(
        messages=[SourceMessage("assistant", "same"), SourceMessage("user", "o"),
                  SourceMessage("assistant", "same"), SourceMessage("assistant", "other")],
        events=[SourceEvent(position=2, at=0.1, name="agent.tool.failed", result="failure"),
                SourceEvent(position=3, at=0.2, name="egress.blocked", result="blocked"),
                SourceEvent(position=3, at=0.3, name="agent.tool.executed", result="success")],
    )
    trace = trace_of(src)
    ev = RulesEvaluator().evaluate(trace)
    assert isinstance(ev, RulesEvaluation)
    proposal_refs = [s.ref for s in trace.steps if s.kind == "proposal"]
    assert ev.redundant_steps == (proposal_refs[1],)
    assert [f.category for f in ev.failures] == ["agent_tool_failed", "egress_blocked"]
    assert ev.efficiency == round(1 - 1 / 3, 3)
    assert (ev.anomaly, ev.anomaly_reason) == (Anomaly.NONE, None)
    assert (ev.task_id, ev.kind, ev.evaluator_id) == (src.task_id, EvaluationKind.POST_HOC, "rules_evaluator")


@pytest.mark.parametrize("counters, expected", [
    ({"violations": 1}, (Anomaly.SUSPICIOUS, "boundary_violation")),
    ({"denials": 2}, (Anomaly.SUSPICIOUS, "repeated_denials")),
    ({"denials": 1}, (Anomaly.NONE, None)),
    ({"violations": 3, "denials": 5}, (Anomaly.SUSPICIOUS, "boundary_violation")),
])
def test_the_anomaly_flag_is_informational_and_deterministic(counters, expected):
    ev = RulesEvaluator().evaluate(trace_of(source(**counters)))
    assert (ev.anomaly, ev.anomaly_reason) == expected


def test_the_same_trace_always_gives_the_same_record():
    src = source(messages=[SourceMessage("assistant", "a"), SourceMessage("assistant", "a")])
    first, second = RulesEvaluator().evaluate(trace_of(src)), RulesEvaluator().evaluate(trace_of(src))
    assert first == second


def test_findings_are_bounded_however_long_the_task():
    msgs = [SourceMessage("assistant", "again") for _ in range(MAX_STEPS)]
    events = [SourceEvent(position=i, at=0.0, name="agent.tool.failed", result="failure") for i in range(300)]
    ev = RulesEvaluator().evaluate(trace_of(source(messages=msgs, events=events)))
    assert len(ev.redundant_steps) <= MAX_REDUNDANT_STEPS and len(ev.failures) <= MAX_FAILURES


def test_the_evaluator_refuses_anything_but_one_trace():
    with pytest.raises(TypeError):
        RulesEvaluator().evaluate([trace_of(source()), trace_of(source())])  # type: ignore[arg-type]


# ── no authority in the record ─────────────────────────────────────────────


AUTHORITY_SHAPED = ("approve", "approved", "authorize", "grant", "grants", "capability", "tier", "confirm",
                    "confirmation", "resume", "stop", "stop_requested", "kill", "prompt", "system_prompt",
                    "improvement_candidates", "rubric", "config", "configuration", "reward", "quality")


def test_the_record_schema_has_no_field_that_could_carry_authority_or_learning():
    fields = set(RulesEvaluation.model_fields)
    assert fields == {"task_id", "evaluator_id", "evaluator_version", "kind", "efficiency",
                      "redundant_steps", "failures", "anomaly", "anomaly_reason"}
    assert not fields & set(AUTHORITY_SHAPED)
    assert {a.value for a in Anomaly} == {"none", "suspicious"}  # no `stop_requested`
    base = dict(task_id=uuid.uuid4(), evaluator_id="rules_evaluator", evaluator_version="1")
    for key in AUTHORITY_SHAPED:
        with pytest.raises(ValidationError):
            RulesEvaluation(**base, **{key: True})
    record = RulesEvaluation(**base)
    with pytest.raises(ValidationError):
        record.anomaly = Anomaly.SUSPICIOUS  # type: ignore[misc] — frozen


def test_the_evaluator_object_holds_no_handle():
    evaluator = RulesEvaluator()
    assert vars(evaluator) == {}  # no model, breaker, store, resolver or config
    assert [n for n in dir(evaluator) if not n.startswith("_")] == ["evaluate", "id", "version"]


# ── the service ────────────────────────────────────────────────────────────


class _Sink:
    def __init__(self) -> None:
        self.records = []

    async def record(self, record):
        self.records.append(record)
        return uuid.uuid4()


async def test_the_service_records_one_task_and_nothing_else():
    sink = _Sink()
    service = EvaluationService(settings=EvaluationSettings(), sink=sink,
                                clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    src = source(request=f"keep {PLANTED['openai']}")
    await service.evaluate(src)
    [record] = sink.records
    assert (record.task_id, record.user_id, record.outcome) == (src.task_id, src.user_id, EvaluationOutcome.RECORDED)
    assert record.redactions and record.evaluation is not None
    assert set(vars(record)) == {"task_id", "user_id", "graph_id", "evaluator_id", "evaluator_version", "kind",
                                 "outcome", "created_at", "reason_code", "evaluation", "redactions"}


async def test_an_evaluator_that_raises_is_recorded_unavailable_and_nothing_more():
    class Broken(RulesEvaluator):
        def evaluate(self, trace):
            raise RuntimeError("boom")

    sink = _Sink()
    await EvaluationService(settings=EvaluationSettings(), sink=sink, evaluator=Broken()).evaluate(source())
    [record] = sink.records
    assert (record.outcome, record.reason_code, record.evaluation) == (
        EvaluationOutcome.UNAVAILABLE, "evaluator_error", None)


async def test_a_record_attributed_to_another_task_is_refused():
    class Misattributing(RulesEvaluator):
        def evaluate(self, trace):
            return super().evaluate(dataclasses.replace(trace, task_id=uuid.uuid4()))

    sink = _Sink()
    await EvaluationService(settings=EvaluationSettings(), sink=sink, evaluator=Misattributing()).evaluate(source())
    assert (sink.records[0].outcome, sink.records[0].reason_code) == (EvaluationOutcome.UNAVAILABLE, "wrong_task")


def test_sampling_always_takes_failures_and_a_fraction_of_the_rest():
    draws = iter([0.05, 0.5])
    service = EvaluationService(settings=EvaluationSettings(sample_successful=0.1), sink=_Sink(),
                                rng=lambda: next(draws))
    assert service.wants(failed=True)
    assert service.wants(failed=False)       # 0.05 < 0.1
    assert not service.wants(failed=False)   # 0.5 >= 0.1
    never = EvaluationService(settings=EvaluationSettings(sample_successful=0.0, always_on_failure=False),
                              sink=_Sink(), rng=lambda: 0.0)
    assert not never.wants(failed=True)


def test_the_service_exposes_no_authority():
    service = EvaluationService(settings=EvaluationSettings(), sink=_Sink())
    public = {n for n in dir(service) if not n.startswith("_")}
    assert public == {"evaluate", "evaluator", "record_for", "settings", "wants"}
