"""`EvaluationService` with fake ports: the §7 failure table, the stop path's
gating, the stop alert (19 §6) and the fail-closed evaluation budget (19 §8)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from server.evaluation.service import EvaluationService, EvaluationSettings
from server.evaluation.trace import SourceMessage, TraceSource
from shared.schemas.evaluation import Anomaly, Evaluation, EvaluationKind, EvaluationOutcome

pytestmark = pytest.mark.asyncio


def source() -> TraceSource:
    return TraceSource(
        task_id=uuid.uuid4(), user_id=uuid.uuid4(), graph_id=None, mode="execute", user_request="r",
        status="running", failure_code=None, final_response=None, unresolved=False, iterations=1,
        model_calls=1, tool_calls=1, worker_switches=0, denials=0, violations=0, rejections=0,
        tripped_source=None, elapsed_seconds=0.1, messages=(SourceMessage("assistant", "p"),),
    )


class FakeSink:
    def __init__(self, *, spent: float = 0.0, fail_reads: bool = False) -> None:
        self.records, self.alerts, self.spent, self.fail_reads = [], [], spent, fail_reads

    async def evaluation_spent_since(self, since):
        if self.fail_reads:
            raise RuntimeError("ledger down")
        return self.spent

    async def global_spent_since(self, since):
        return 0.0

    async def record(self, record):
        self.records.append(record)
        return uuid.uuid4()

    async def stop_alert(self, *, evaluator_id, stops_in_window):
        self.alerts.append(stops_in_window)

    async def rubric(self):
        return None


class Switches:
    def __init__(self, judge=True, stop=True):
        self.judge, self.stop = judge, stop

    def judge_enabled(self):
        return self.judge

    def stop_requests_enabled(self):
        return self.stop


class Breaker:
    def __init__(self, live=True):
        self.trips, self.live = [], live

    async def trip(self, *, task_id, reason):
        self.trips.append((task_id, reason))
        return self.live


class Provider:
    id, version = "fake_judge", "1"

    def __init__(self, anomaly=Anomaly.STOP_REQUESTED, *, task_id=None, kind=None, cost_check=None):
        self.anomaly, self.task_id, self.kind, self.cost_check, self.calls = anomaly, task_id, kind, cost_check, 0

    async def health(self):
        return True

    async def evaluate(self, trace, kind):
        self.calls += 1
        if self.cost_check is not None:
            from server.evaluation.metering import CURRENT_JUDGE_METER

            await CURRENT_JUDGE_METER.get().precheck(self.cost_check)
        return Evaluation(task_id=self.task_id or trace.task_id, evaluator_id=self.id, evaluator_version="1",
                          kind=self.kind or kind, anomaly=self.anomaly,
                          anomaly_reason=None if self.anomaly is Anomaly.NONE else "weird")


def service(provider=None, *, sink=None, switches=None, breaker=None, clock=None, **settings):
    return EvaluationService(
        provider=provider or Provider(), settings=EvaluationSettings(**settings), sink=sink or FakeSink(),
        switches=switches or Switches(), breaker=breaker,
        clock=clock or (lambda: datetime.now(timezone.utc)), rng=lambda: 0.5,
    )


async def test_disabled_runs_nothing():
    provider, sink = Provider(), FakeSink()
    report = await service(provider, sink=sink, switches=Switches(judge=False)).evaluate(
        source(), EvaluationKind.POST_HOC)
    assert report.outcome is None and provider.calls == 0 and sink.records == []


@pytest.mark.parametrize("kind, switches, breaker, honoured, because", [
    (EvaluationKind.LIVE_WINDOW, Switches(), Breaker(), True, None),
    (EvaluationKind.LIVE_WINDOW, Switches(stop=False), Breaker(), False, "stop_requests_disabled"),
    (EvaluationKind.LIVE_WINDOW, Switches(), None, False, "stop_requests_disabled"),
    (EvaluationKind.LIVE_WINDOW, Switches(), Breaker(live=False), False, "task_not_live"),
    (EvaluationKind.POST_HOC, Switches(), Breaker(), False, "post_hoc"),
])
async def test_a_stop_request_reaches_the_breaker_only_when_every_gate_allows(kind, switches, breaker,
                                                                              honoured, because):
    sink = FakeSink()
    report = await service(sink=sink, switches=switches, breaker=breaker).evaluate(source(), kind)
    assert (report.stop.honoured, report.stop.not_honoured_because) == (honoured, because)
    tripped = bool(breaker and breaker.trips)
    assert tripped == (kind is EvaluationKind.LIVE_WINDOW and switches.stop and breaker is not None)
    assert sink.records[0].stop.requested


async def test_the_stop_alert_fires_above_the_threshold_once_per_window():
    now = [datetime(2026, 9, 27, tzinfo=timezone.utc)]
    sink, breaker = FakeSink(), Breaker()
    svc = service(sink=sink, breaker=breaker, clock=lambda: now[0], stop_alert_threshold=2,
                  stop_alert_window=timedelta(hours=1))
    for _ in range(4):
        await svc.evaluate(source(), EvaluationKind.LIVE_WINDOW)
        now[0] += timedelta(minutes=1)
    assert sink.alerts == [3]  # the third stop crossed the threshold; the fourth is in the same window
    now[0] += timedelta(hours=2)
    await svc.evaluate(source(), EvaluationKind.LIVE_WINDOW)
    assert sink.alerts == [3]  # the window emptied; one stop is under the threshold


async def test_an_answer_attributed_to_another_task_is_malformed():
    sink = FakeSink()
    report = await service(Provider(Anomaly.STOP_REQUESTED, task_id=uuid.uuid4()), sink=sink,
                           breaker=Breaker()).evaluate(source(), EvaluationKind.LIVE_WINDOW)
    assert (report.outcome, sink.records[0].reason_code) == (EvaluationOutcome.MALFORMED, "wrong_task")
    assert not report.stop.requested


async def test_an_unreadable_evaluation_ledger_is_over_budget():
    sink = FakeSink(fail_reads=True)
    report = await service(Provider(Anomaly.NONE, cost_check=0.5), sink=sink, budget=10.0).evaluate(
        source(), EvaluationKind.POST_HOC)
    assert (report.outcome, sink.records[0].reason_code) == (EvaluationOutcome.OVER_BUDGET, "limiter_unavailable")


async def test_the_evaluation_budget_counts_prior_judge_spend():
    sink = FakeSink(spent=0.9)
    report = await service(Provider(Anomaly.NONE, cost_check=0.2), sink=sink, budget=1.0,
                           budget_scope="own").evaluate(source(), EvaluationKind.POST_HOC)
    assert report.outcome is EvaluationOutcome.OVER_BUDGET


async def test_sampling_always_takes_failures_and_a_fraction_of_the_rest():
    svc = service(sample_successful=0.4, always_on_failure=True)
    assert svc.wants_post_hoc(failed=True) is True
    assert svc.wants_post_hoc(failed=False) is False  # rng 0.5 ≥ 0.4
    assert service(sample_successful=0.6).wants_post_hoc(failed=False) is True
    assert service(switches=Switches(judge=False)).wants_post_hoc(failed=True) is False


async def test_the_service_exposes_no_authority():
    svc = service()
    public = {name for name in dir(svc) if not name.startswith("_")}
    assert public == {"evaluate", "provider", "settings", "wants_post_hoc"}
