"""One evaluation of one task, start to finish (19 §4–§8).

    TraceSource ─ build_trace (redact, bound, window) ─▶ EvaluationProvider
        ─▶ Evaluation (validated) ─▶ candidates checked against the registry
        ─▶ stop_requested? ─▶ breaker.trip()            (only if opted in, live only)
        ─▶ record: evaluation, pending candidates, usage, audit

Failure behaviour (19 §7), every case recorded and none of them touching the
task:

| Judge condition           | Outcome        |
|---------------------------|----------------|
| disabled                  | nothing runs   |
| unavailable / timeout     | `unavailable`  |
| malformed output          | `malformed`    |
| over budget               | `over_budget`  |

The order of the last two steps is deliberate. A stop goes to the breaker in
memory *before* anything is written: the task being stopped may be running in
another request that holds the store's write lock (the pilot's SQLite has one
writer), exactly as for an operator stop (18 §5.4).

`quality`, `efficiency` and `reward` are recorded and read by nothing that
decides anything.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from server.evaluation.candidates import CandidateRejected, split_target, validate_candidate
from server.evaluation.metering import (
    CURRENT_JUDGE_METER,
    JudgeOverBudget,
    RecordingMeter,
    UnmeteredJudgeCall,
)
from server.evaluation.ports import (
    BreakerTripPort,
    EvaluationRecord,
    EvaluationSink,
    EvaluationSwitches,
    RejectedCandidate,
    StopDecision,
)
from server.evaluation.provider import EvaluationProvider, EvaluationUnavailable, MalformedEvaluation
from server.evaluation.trace import TraceSource, build_trace
from shared.schemas.evaluation import Anomaly, EvaluationKind, EvaluationOutcome

logger = logging.getLogger("hypermind.evaluation")

BUDGET_WINDOW = timedelta(days=1)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class EvaluationSettings:
    """The subset of `evaluation.*` (and the global budget) the service uses."""

    budget: float = 0.0
    budget_scope: str = "own_and_global"
    global_daily_cost_limit: float = 0.0
    sample_successful: float = 0.1
    always_on_failure: bool = True
    live_window_steps: int = 20
    max_observation_chars: int = 2000
    stop_alert_threshold: int = 5
    stop_alert_window: timedelta = timedelta(hours=1)
    timeout_seconds: float = 60.0


@dataclass(frozen=True)
class EvaluationReport:
    outcome: EvaluationOutcome | None  # None: not evaluated (disabled / not sampled)
    evaluation_id: uuid.UUID | None = None
    stop: StopDecision = StopDecision()
    skipped_because: str | None = None


class EvaluationService:
    def __init__(
        self,
        *,
        provider: EvaluationProvider,
        settings: EvaluationSettings,
        sink: EvaluationSink,
        switches: EvaluationSwitches,
        breaker: BreakerTripPort | None,
        clock: Callable[[], datetime] = _utcnow,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._provider = provider
        self._settings = settings
        self._sink = sink
        self._switches = switches
        # `None` unless the operator opted in (`may_request_stop`): without it
        # this package holds no handle on the breaker at all.
        self._breaker = breaker
        self._clock = clock
        self._rng = rng
        self._stops: deque[datetime] = deque()
        self._alerted_at: datetime | None = None

    @property
    def provider(self) -> EvaluationProvider:
        return self._provider

    @property
    def settings(self) -> EvaluationSettings:
        return self._settings

    def wants_post_hoc(self, *, failed: bool) -> bool:
        """19 §8 sampling: every failed task (if configured), a fraction of the rest."""

        if not self._switches.judge_enabled():
            return False
        if failed and self._settings.always_on_failure:
            return True
        return self._rng() < self._settings.sample_successful

    async def evaluate(self, source: TraceSource, kind: EvaluationKind) -> EvaluationReport:
        if not self._switches.judge_enabled():
            return EvaluationReport(outcome=None, skipped_because="disabled")

        trace = build_trace(
            source, kind=kind, max_chars=self._settings.max_observation_chars,
            window_steps=self._settings.live_window_steps if kind is EvaluationKind.LIVE_WINDOW else None,
        )
        meter = RecordingMeter(budget_check=self._budget_check)
        outcome, reason, evaluation = EvaluationOutcome.RECORDED, None, None
        token = CURRENT_JUDGE_METER.set(meter)
        try:
            evaluation = await asyncio.wait_for(
                self._provider.evaluate(trace, kind), timeout=self._settings.timeout_seconds
            )
        except JudgeOverBudget as exc:
            outcome, reason = EvaluationOutcome.OVER_BUDGET, exc.limit
        except MalformedEvaluation as exc:
            outcome, reason = EvaluationOutcome.MALFORMED, exc.code
        except (EvaluationUnavailable, asyncio.TimeoutError):
            outcome, reason = EvaluationOutcome.UNAVAILABLE, "provider_unavailable"
        except UnmeteredJudgeCall:
            logger.error("a Judge model call had no meter; refused")
            outcome, reason = EvaluationOutcome.UNAVAILABLE, "unmetered_call_refused"
        except Exception:  # noqa: BLE001 — the Judge never fails anything else
            logger.exception("the evaluator raised; recorded as unavailable")
            outcome, reason = EvaluationOutcome.UNAVAILABLE, "provider_error"
        finally:
            CURRENT_JUDGE_METER.reset(token)

        if evaluation is not None and (evaluation.task_id != trace.task_id or evaluation.kind is not kind):
            # A provider may not attribute its answer to another task.
            outcome, reason, evaluation = EvaluationOutcome.MALFORMED, "wrong_task", None

        accepted, rejected = [], []
        stop = StopDecision()
        if evaluation is not None:
            for candidate in evaluation.improvement_candidates:
                try:
                    accepted.append(validate_candidate(candidate, task_id=str(trace.task_id)))
                except CandidateRejected as exc:
                    head = split_target(candidate.target)[0].split(".", 1)[0][:32].lower()
                    rejected.append(RejectedCandidate(code=exc.code, category=exc.category,
                                                      target=head if head.isidentifier() else "invalid"))
            if evaluation.anomaly is Anomaly.STOP_REQUESTED:
                stop = await self._request_stop(trace.task_id, evaluation.anomaly_reason or "anomaly", kind)

        record = EvaluationRecord(
            task_id=trace.task_id, user_id=trace.user_id, graph_id=trace.graph_id,
            evaluator_id=self._provider.id, evaluator_version=self._provider.version, kind=kind,
            outcome=outcome, created_at=self._clock(), reason_code=reason, evaluation=evaluation,
            accepted_candidates=tuple(accepted), rejected_candidates=tuple(rejected),
            usage=tuple(meter.records), redactions=trace.redactions, stop=stop,
        )
        evaluation_id = await self._sink.record(record)
        if stop.honoured:
            await self._maybe_alert()
        return EvaluationReport(outcome=outcome, evaluation_id=evaluation_id, stop=stop)

    # ── the one runtime-facing effect (19 §6) ──────────────────────────

    async def _request_stop(self, task_id: uuid.UUID, reason: str, kind: EvaluationKind) -> StopDecision:
        if kind is not EvaluationKind.LIVE_WINDOW:
            return StopDecision(requested=True, reason=reason, not_honoured_because="post_hoc")
        if self._breaker is None or not self._switches.stop_requests_enabled():
            return StopDecision(requested=True, reason=reason, not_honoured_because="stop_requests_disabled")
        tripped = await self._breaker.trip(task_id=task_id, reason=reason)
        if not tripped:
            return StopDecision(requested=True, reason=reason, not_honoured_because="task_not_live")
        self._stops.append(self._clock())
        return StopDecision(requested=True, honoured=True, reason=reason)

    async def _maybe_alert(self) -> None:
        now = self._clock()
        window = self._settings.stop_alert_window
        while self._stops and now - self._stops[0] > window:
            self._stops.popleft()
        if len(self._stops) <= self._settings.stop_alert_threshold:
            return
        if self._alerted_at is not None and now - self._alerted_at < window:
            return
        self._alerted_at = now
        logger.warning("evaluator %s stopped %d tasks within %s; the operator may disable the Judge",
                       self._provider.id, len(self._stops), window)
        await self._sink.stop_alert(evaluator_id=self._provider.id, stops_in_window=len(self._stops))

    # ── the Judge's own budget (19 §8) ─────────────────────────────────

    async def _budget_check(self, projected_cost: float) -> None:
        if projected_cost <= 0:
            return
        since = self._clock() - BUDGET_WINDOW
        try:
            spent = await self._sink.evaluation_spent_since(since)
            if spent + projected_cost > self._settings.budget:
                raise JudgeOverBudget("evaluation_budget")
            if self._settings.budget_scope == "own_and_global":
                if await self._sink.global_spent_since(since) + projected_cost > self._settings.global_daily_cost_limit:
                    raise JudgeOverBudget("global_budget")
        except JudgeOverBudget:
            raise
        except Exception:  # noqa: BLE001 — an unreadable ledger is at-limit (13 §4)
            logger.exception("evaluation ledger unreadable; treating as over budget")
            raise JudgeOverBudget("limiter_unavailable") from None


__all__ = ["EvaluationReport", "EvaluationService", "EvaluationSettings"]
