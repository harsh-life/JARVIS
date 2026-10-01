"""Wiring the Judge to the runtime (19) — the only place the two meet.

    AgentRuntime ──observer──▶ JudgeObserver ──enqueue──▶ EvaluationQueue
                                                              │  (its own task,
                                                              ▼   its own context)
                          EvaluationService.evaluate(TraceSource, kind)
                              │                          │
                   RuntimeBreakerPort.trip()     StorageEvaluationSink.record()
                   (only if may_request_stop)    (evaluation, candidates,
                                                  usage, audit — one transaction)

The runtime notifies and never waits (19 §6); the Judge reads a copy of the
task and never a live handle; its one way to affect a running task is the
breaker's `trip()`, which is handed to it only when the operator opted in, and
whose enforcement is the runtime's own deterministic stop sequence (18 §5.3).

Two constraints of the pilot's store shape the sink:

* **One writer.** A running task's request holds SQLite's write lock until it
  stops. So a live evaluation trips the breaker *in memory first*, and every
  write the evaluation owes is made at the end, in one transaction, retried
  while the store is busy — up to `persist_timeout_seconds`, which outlasts
  any task's wall clock.
* **No request context.** The worker runs in a fresh `contextvars.Context`, so
  no request's secret resolver leaks into it. A Judge whose key is a
  SecretStore handle gets a resolver bound to the job's own read session, with
  the `model_api_key`-class restriction the request path has, and its
  `secret.get` audit rows are written with the evaluation's.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from server.agent import AgentRuntime
from server.agent.breaker import TripSource
from server.agent.ports import TaskSnapshot
from server.agent.state import TaskState
from server.composition.break_glass import BreakGlassRegistry, EndReason
from server.composition.improvements import EvaluationSwitchboard, TuningCache
from server.composition.models import ProviderFactory, spec_from_entry
from server.composition.secret_context import CURRENT_SECRET_RESOLVER, SecretUnavailable, key_provider_for
from server.config.schema import AppConfig
from server.evaluation.candidates import OWNER_SCOPED_TARGETS
from server.evaluation.metering import MeteredJudgeModel
from server.evaluation.ports import EvaluationRecord
from server.evaluation.provider import EvaluationProvider, LLMJudge, RulesJudge
from server.evaluation.runner import EvaluationQueue
from server.evaluation.service import EvaluationReport, EvaluationService, EvaluationSettings
from server.evaluation.trace import SourceEvent, SourceMessage, TraceSource
from server.secrets.audit_port import SecretAuditEvent
from server.secrets.requester import SecretRequester
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.security.usage import UsageLedger
from server.storage import StorageBackend
from server.storage.errors import is_transient_store_error
from server.storage.models import ImprovementCandidateRow, SecretReference, TaskEvaluation, UsageEvent
from shared.schemas.enums import AuditActor, AuditResult, SecretClass, UsageKind, Visibility
from shared.schemas.evaluation import (
    EVALUATOR_USAGE_PREFIX,
    CandidateStatus,
    EvaluationKind,
    EvaluationOutcome,
)

logger = logging.getLogger("hypermind.composition.evaluation")

_OUTCOME_ACTIONS = {
    EvaluationOutcome.RECORDED: AuditAction.EVALUATION_RECORDED,
    EvaluationOutcome.UNAVAILABLE: AuditAction.EVALUATION_UNAVAILABLE,
    EvaluationOutcome.MALFORMED: AuditAction.EVALUATION_REJECTED,
    EvaluationOutcome.OVER_BUDGET: AuditAction.EVALUATION_SKIPPED,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── persistence ────────────────────────────────────────────────────────────


class StorageEvaluationSink:
    """Implements `server.evaluation.ports.EvaluationSink`."""

    def __init__(self, storage: StorageBackend, tuning: TuningCache, *, persist_timeout_seconds: float) -> None:
        self._storage = storage
        self._tuning = tuning
        self._persist_timeout = persist_timeout_seconds
        # Secret-resolution audit rows owed by the evaluation in progress
        # (bound per job; see `EvaluationJobs`).
        self.pending_secret_events: contextvars.ContextVar[list[SecretAuditEvent] | None] = \
            contextvars.ContextVar("hypermind_judge_secret_events", default=None)

    async def evaluation_spent_since(self, since: datetime) -> float:
        async with self._storage.session() as session:
            return float((await session.execute(
                select(func.coalesce(func.sum(UsageEvent.estimated_cost), 0.0)).where(
                    UsageEvent.timestamp >= since, UsageEvent.tool_id.like(f"{EVALUATOR_USAGE_PREFIX}%")
                )
            )).scalar_one())

    async def global_spent_since(self, since: datetime) -> float:
        async with self._storage.session() as session:
            return await UsageLedger().cost_since(session, since=since)

    async def rubric(self) -> str | None:
        return await self._tuning.rubric()

    async def record(self, record: EvaluationRecord) -> uuid.UUID:
        evaluation_id = uuid.uuid4()
        secret_events = list(self.pending_secret_events.get() or [])

        async def write(session: AsyncSession) -> None:
            audit = AuditLogger(session, request_id=evaluation_id)
            for event in secret_events:
                await audit.record_secret_event(event)
            await self._write(session, audit, record, evaluation_id)

        await self.persist(write)
        return evaluation_id

    async def stop_alert(self, *, evaluator_id: str, stops_in_window: int) -> None:
        async def write(session: AsyncSession) -> None:
            await AuditLogger(session, request_id=uuid.uuid4()).record(
                actor=AuditActor.SYSTEM, action=AuditAction.EVALUATION_STOP_ALERT,
                resource=f"evaluator:{evaluator_id}:stops:{stops_in_window}", result=AuditResult.FAILURE,
            )

        await self.persist(write)

    async def persist(self, write: Callable[[AsyncSession], Awaitable[None]]) -> None:
        """One transaction, retried while the store is busy (a running task's
        request holds the pilot's single write lock until it stops)."""

        deadline = asyncio.get_running_loop().time() + self._persist_timeout
        delay = 0.05
        while True:
            try:
                async with self._storage.session() as session:
                    await write(session)
                    await session.commit()
                return
            except DBAPIError as exc:
                if not is_transient_store_error(exc) or asyncio.get_running_loop().time() >= deadline:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 1.0)

    @staticmethod
    async def _write(session: AsyncSession, audit: AuditLogger, record: EvaluationRecord,
                     evaluation_id: uuid.UUID) -> None:
        ev = record.evaluation
        findings = None
        if ev is not None:
            findings = {
                "redundant_steps": list(ev.redundant_steps),
                "failures": [f.model_dump() for f in ev.failures],
                "reward": ev.reward.model_dump() if ev.reward is not None else None,
            }
        session.add(TaskEvaluation(
            evaluation_id=evaluation_id, task_id=record.task_id, owner_user_id=record.user_id,
            graph_id=record.graph_id, visibility=Visibility.PRIVATE,
            evaluator_id=record.evaluator_id, evaluator_version=record.evaluator_version,
            kind=record.kind.value, outcome=record.outcome.value, reason_code=record.reason_code,
            quality=ev.quality if ev else None, efficiency=ev.efficiency if ev else None,
            anomaly=ev.anomaly.value if ev else "none", anomaly_reason=ev.anomaly_reason if ev else None,
            findings=findings, stop_requested=record.stop.requested, stop_honoured=record.stop.honoured,
            stop_not_honoured_because=record.stop.not_honoured_because,
            redactions=list(record.redactions) or None, created_at=record.created_at,
        ))
        # 19 §8: one usage row per Judge model call, attributed to the
        # evaluator — the task's owner for the record, but excluded from that
        # user's own budget and rates (server/security/usage.py).
        ledger = UsageLedger()
        for usage in record.usage:
            await ledger.record(
                session, request_id=evaluation_id, user_id=record.user_id, kind=UsageKind.MODEL_CALL,
                units=usage.units, estimated_cost=usage.cost, provider=usage.provider, model=usage.model,
                tool_id=f"{EVALUATOR_USAGE_PREFIX}{record.evaluator_id}", graph_id=record.graph_id,
            )
        await session.flush()

        def row(action: AuditAction, resource: str, result: AuditResult) -> Awaitable[None]:
            return audit.record(actor=AuditActor.SYSTEM, action=action, resource=resource[:128], result=result,
                                user_id=record.user_id, graph_id=record.graph_id, flush=False)

        for name in record.redactions:
            await row(AuditAction.EVALUATION_TRACE_REDACTED, f"evaluation:{evaluation_id}:{name}",
                      AuditResult.BLOCKED)
        detail = record.reason_code or (ev.anomaly.value if ev else "none")
        await row(_OUTCOME_ACTIONS[record.outcome],
                  f"evaluation:{evaluation_id}:task:{record.task_id}:{record.kind.value}:{detail}",
                  AuditResult.SUCCESS if record.outcome is EvaluationOutcome.RECORDED else AuditResult.FAILURE)
        if record.stop.requested:
            await row(AuditAction.EVALUATION_STOP_REQUESTED,
                      f"evaluation:{evaluation_id}:{record.stop.reason}:"
                      + ("honoured" if record.stop.honoured else record.stop.not_honoured_because or "not_honoured"),
                      AuditResult.SUCCESS if record.stop.honoured else AuditResult.BLOCKED)
        for accepted in record.accepted_candidates:
            candidate_id = uuid.uuid4()
            session.add(ImprovementCandidateRow(
                candidate_id=candidate_id, evaluation_id=evaluation_id, task_id=record.task_id,
                source_user_id=record.user_id, evaluator_id=record.evaluator_id, target=accepted.target,
                # docs/29 §18: an owner-scoped candidate names its agent, from
                # the trace — never from anything the Judge wrote.
                agent_id=record.agent_id if accepted.target in OWNER_SCOPED_TARGETS else None,
                subject=accepted.subject, proposed_value=str(accepted.value),
                expected_effect=accepted.expected_effect, evidence=list(accepted.evidence),
                status=CandidateStatus.PENDING.value, created_at=record.created_at,
            ))
            await row(AuditAction.IMPROVEMENT_CANDIDATE_QUEUED,
                      f"candidate:{candidate_id}:{accepted.full_target}", AuditResult.SUCCESS)
        for rejected in record.rejected_candidates:
            await row(AuditAction.IMPROVEMENT_CANDIDATE_REFUSED,
                      f"evaluation:{evaluation_id}:{rejected.code}:{rejected.category or rejected.target}",
                      AuditResult.BLOCKED)
        await session.flush()


# ── the one runtime-facing effect ──────────────────────────────────────────


class RuntimeBreakerPort:
    """Implements `server.evaluation.ports.BreakerTripPort` with the runtime's
    own breaker. Stop only: it trips in memory (a running task's request then
    enforces the stop at its next checkpoint, aborting any in-flight call), ends
    the task's break-glass record at once (20 §2.4), and — for a paused task,
    which nobody is driving — runs the same enforcement sequence here."""

    def __init__(self, *, runtime: AgentRuntime, environment: Callable, storage: StorageBackend,
                 break_glass: BreakGlassRegistry | None, persist: Callable) -> None:
        self._runtime = runtime
        self._environment = environment
        self._storage = storage
        self._break_glass = break_glass
        self._persist = persist

    async def trip(self, *, task_id: uuid.UUID, reason: str) -> bool:
        if not self._runtime.signal_stop(task_id, reason=reason, source=TripSource.EVALUATOR.value):
            return False
        if self._break_glass is not None:
            self._break_glass.end_task(task_id, EndReason.BREAKER_TRIP)
        state: TaskState | None = self._runtime.states.get(task_id)
        if state is not None and (state.pending is not None or state.platform_wait is not None):
            async def enforce(session: AsyncSession) -> None:
                env = self._environment(session, AuditLogger(session, request_id=uuid.uuid4()))
                await self._runtime.enforce_trip(env, task_id)

            try:
                await self._persist(enforce)
            except Exception:  # noqa: BLE001 — the trip stands; the owner's next /confirm enforces it
                logger.exception("could not enforce the evaluator's stop on paused task %s now", task_id)
        return True


# ── the observer the runtime notifies ──────────────────────────────────────


def trace_source(snapshot: TaskSnapshot) -> TraceSource:
    return TraceSource(
        task_id=snapshot.task_id, user_id=snapshot.user_id, graph_id=snapshot.graph_id, mode=snapshot.mode,
        user_request=snapshot.user_input, status=snapshot.status, failure_code=snapshot.failure_code,
        final_response=snapshot.final_response, unresolved=snapshot.unresolved,
        iterations=snapshot.iterations, model_calls=snapshot.model_calls, tool_calls=snapshot.tool_calls,
        worker_switches=snapshot.worker_switches, denials=snapshot.denials, violations=snapshot.violations,
        rejections=snapshot.rejections, tripped_source=snapshot.tripped_source,
        elapsed_seconds=snapshot.elapsed_seconds,
        messages=tuple(SourceMessage(role, content) for role, content in snapshot.transcript),
        events=tuple(SourceEvent(**vars(e)) for e in snapshot.events),
        agent_id=snapshot.agent_id, agent_run_id=snapshot.agent_run_id, agent_version=snapshot.agent_version,
    )


class JudgeObserver:
    """Implements `server.agent.ports.TaskObserver`: enqueue, never block."""

    def __init__(self, *, jobs: "EvaluationJobs", config: AppConfig) -> None:
        self._jobs = jobs
        self._live = config.evaluation.live
        self._live_in_flight: set[uuid.UUID] = set()

    def step_completed(self, task_id: uuid.UUID, tool_calls: int, snapshot: Callable[[], TaskSnapshot]) -> None:
        if not self._live.enabled or not self._jobs.switches.judge_enabled():
            return
        if tool_calls % self._live.every_n_steps or task_id in self._live_in_flight:
            return
        source = trace_source(snapshot())
        self._live_in_flight.add(task_id)

        async def run() -> None:
            try:
                await self._jobs.run(source, EvaluationKind.LIVE_WINDOW)
            finally:
                self._live_in_flight.discard(task_id)

        if not self._jobs.queue.submit(f"live:{task_id}", run):
            self._live_in_flight.discard(task_id)

    def task_ended(self, snapshot: TaskSnapshot) -> None:
        failed = snapshot.status == "failed" or snapshot.unresolved
        if not self._jobs.service.wants_post_hoc(failed=failed):
            return
        source = trace_source(snapshot)
        self._jobs.queue.submit(f"post_hoc:{snapshot.task_id}",
                                lambda: self._jobs.run(source, EvaluationKind.POST_HOC))


class EvaluationJobs:
    """Runs one evaluation in the queue's own context."""

    def __init__(self, *, service: EvaluationService, queue: EvaluationQueue, switches: EvaluationSwitchboard,
                 storage: StorageBackend, sink: StorageEvaluationSink, secret_store) -> None:
        self.service = service
        self.queue = queue
        self.switches = switches
        self._storage = storage
        self._sink = sink
        self._secret_store = secret_store
        self.reports: list[EvaluationReport] = []

    async def run(self, source: TraceSource, kind: EvaluationKind) -> EvaluationReport:
        await self.switches.ensure_loaded(self._storage)
        owed: list[SecretAuditEvent] = []
        events_token = self._sink.pending_secret_events.set(owed)
        async with self._storage.session() as session:
            resolver_token = CURRENT_SECRET_RESOLVER.set(self._resolver(session, owed))
            try:
                report = await self.service.evaluate(source, kind)
            finally:
                CURRENT_SECRET_RESOLVER.reset(resolver_token)
                self._sink.pending_secret_events.reset(events_token)
            await session.rollback()  # read-only; nothing of this session is kept
        self.reports.append(report)
        del self.reports[:-100]
        return report

    def _resolver(self, session: AsyncSession, owed: list[SecretAuditEvent]):
        store = self._secret_store

        class _Buffered:
            async def record_secret_event(self, event: SecretAuditEvent) -> None:
                owed.append(event)

        async def resolve(handle: str, requester: SecretRequester) -> str:
            reference = await session.get(SecretReference, handle)
            if reference is not None and reference.class_ is not SecretClass.MODEL_API_KEY:
                owed.append(SecretAuditEvent(action="secret.get", secret_ref=handle,
                                             requester=requester.describe(), result=AuditResult.BLOCKED,
                                             reason="not_a_model_api_key"))
                raise SecretUnavailable()
            return await store.get(session, handle, requester, _Buffered())

        return resolve


# ── assembly ───────────────────────────────────────────────────────────────


def build_judge(config: AppConfig, factory: ProviderFactory, tuning: TuningCache) -> EvaluationProvider:
    section = config.evaluation
    if section.evaluator == "rules":
        return RulesJudge()
    entry = section.provider
    assert entry is not None  # the config schema requires it for `llm`
    model = factory(spec_from_entry(entry), key_provider=key_provider_for(entry.secret_ref, SecretRequester.server()))
    return LLMJudge(MeteredJudgeModel(model), rubric=tuning.rubric)


def build_evaluation(
    config: AppConfig, *, runtime: AgentRuntime, storage: StorageBackend, factory: ProviderFactory,
    tuning: TuningCache, switches: EvaluationSwitchboard, environment: Callable,
    break_glass: BreakGlassRegistry | None, secret_store, judge: EvaluationProvider | None = None,
) -> tuple[JudgeObserver, EvaluationJobs] | None:
    """`None` when `evaluation.enabled` is false: no observer, no queue, no
    breaker handle — the runtime runs exactly as without a Judge (JDG-T1)."""

    section = config.evaluation
    if not section.enabled:
        return None
    provider = judge or build_judge(config, factory, tuning)
    if section.evaluator == "llm" and section.provider is not None and section.provider.provider != "ollama":
        # 19 §4: a cloud Judge receives task traces. A disclosed choice, never silent.
        logger.warning("evaluation.provider %r is not local: redacted task traces are sent to it (docs/19 §4)",
                       section.provider.provider)
    persist_timeout = config.agent.bounds.wall_clock_timeout_seconds + 120.0
    sink = StorageEvaluationSink(storage, tuning, persist_timeout_seconds=persist_timeout)
    breaker = None
    if section.may_request_stop:
        breaker = RuntimeBreakerPort(runtime=runtime, environment=environment, storage=storage,
                                     break_glass=break_glass, persist=sink.persist)
    timeout = (section.provider.timeout_seconds if section.provider is not None else 30.0) + 5.0
    service = EvaluationService(
        provider=provider,
        settings=EvaluationSettings(
            budget=section.budget, budget_scope=section.budget_scope,
            global_daily_cost_limit=config.security.budgets.global_daily_cost_limit,
            sample_successful=section.post_hoc.sample_successful,
            always_on_failure=section.post_hoc.always_on_failure,
            live_window_steps=section.live.window_steps, max_observation_chars=section.max_observation_chars,
            stop_alert_threshold=section.stop_alert_threshold,
            stop_alert_window=timedelta(minutes=section.stop_alert_window_minutes), timeout_seconds=timeout,
        ),
        sink=sink, switches=switches, breaker=breaker,
    )
    queue = EvaluationQueue(maxsize=section.queue_size)
    jobs = EvaluationJobs(service=service, queue=queue, switches=switches, storage=storage, sink=sink,
                          secret_store=secret_store)
    return JudgeObserver(jobs=jobs, config=config), jobs


__all__ = [
    "EvaluationJobs",
    "JudgeObserver",
    "RuntimeBreakerPort",
    "StorageEvaluationSink",
    "build_evaluation",
    "build_judge",
    "trace_source",
]
