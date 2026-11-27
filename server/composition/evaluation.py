"""Wiring the rules evaluator to the runtime — the only place the two meet.

    AgentRuntime ──observer.task_ended──▶ RulesObserver ──enqueue──▶ EvaluationQueue
                                                                       │ (its own task,
                                                                       ▼  its own context)
                               EvaluationService.evaluate(TraceSource)
                                           │
                               StorageEvaluationSink.record()
                               (evaluation row + audit — one transaction)

The runtime notifies and never waits; the evaluator reads a copy of the
finished task and never a live handle. It has **no** effect on any task: this
module hands it no breaker, no secret resolver, no model, no switchboard and
no prompt or configuration writer. The record is informational.

Retired here (docs/DECISION_REGISTER.md, D-JDG-RETIRE): the LLM Judge's
provider assembly, its metered model and SecretStore key resolution, the
live-window observer, the breaker port for stop requests, the operator
switchboard, and the writer of improvement candidates. The
`improvement_candidates`, `config_versions` and `evaluation_control` tables
remain in the schema for historical records only; nothing in the server
writes them or reads them to change behaviour.

One constraint of the pilot's store shapes the sink: **one writer**. A running
task's request holds SQLite's write lock until it stops, so the write is made
from the queue, in one transaction, retried while the store is busy — up to
`persist_timeout_seconds`, which outlasts any task's wall clock.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Awaitable, Callable

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from server.agent.ports import TaskSnapshot
from server.config.schema import AppConfig
from server.evaluation.ports import EvaluationRecord
from server.evaluation.runner import EvaluationQueue
from server.evaluation.service import EvaluationService, EvaluationSettings
from server.evaluation.trace import SourceEvent, SourceMessage, TraceSource
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.storage import StorageBackend
from server.storage.errors import is_transient_store_error
from server.storage.models import TaskEvaluation
from shared.schemas.enums import AuditActor, AuditResult, Visibility
from shared.schemas.evaluation import EvaluationOutcome

logger = logging.getLogger("hypermind.composition.evaluation")

_OUTCOME_ACTIONS = {
    EvaluationOutcome.RECORDED: AuditAction.EVALUATION_RECORDED,
    EvaluationOutcome.UNAVAILABLE: AuditAction.EVALUATION_UNAVAILABLE,
}


# ── persistence ────────────────────────────────────────────────────────────


class StorageEvaluationSink:
    """Implements `server.evaluation.ports.EvaluationSink`. Writes exactly one
    `evaluations` row and its audit rows — never a candidate, a configuration
    version, a usage row or anything another user's task reads."""

    def __init__(self, storage: StorageBackend, *, persist_timeout_seconds: float) -> None:
        self._storage = storage
        self._persist_timeout = persist_timeout_seconds

    async def record(self, record: EvaluationRecord) -> uuid.UUID:
        evaluation_id = uuid.uuid4()

        async def write(session: AsyncSession) -> None:
            await self._write(session, AuditLogger(session, request_id=evaluation_id), record, evaluation_id)

        await self.persist(write)
        return evaluation_id

    async def persist(self, write: Callable[[AsyncSession], Awaitable[None]]) -> None:
        """One transaction, retried while the store is busy."""

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
            }
        session.add(TaskEvaluation(
            evaluation_id=evaluation_id, task_id=record.task_id, owner_user_id=record.user_id,
            graph_id=record.graph_id, visibility=Visibility.PRIVATE,
            evaluator_id=record.evaluator_id, evaluator_version=record.evaluator_version,
            kind=record.kind.value, outcome=record.outcome.value, reason_code=record.reason_code,
            quality=None, efficiency=ev.efficiency if ev else None,
            anomaly=ev.anomaly.value if ev else "none", anomaly_reason=ev.anomaly_reason if ev else None,
            findings=findings, stop_requested=False, stop_honoured=False, stop_not_honoured_because=None,
            redactions=list(record.redactions) or None, created_at=record.created_at,
        ))
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
        await session.flush()


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


class RulesObserver:
    """Implements `server.agent.ports.TaskObserver`: enqueue a post-hoc
    evaluation of a finished task, never block, never read anything back."""

    def __init__(self, *, jobs: "EvaluationJobs") -> None:
        self._jobs = jobs

    def step_completed(self, task_id: uuid.UUID, tool_calls: int, snapshot: Callable[[], TaskSnapshot]) -> None:
        return None  # evaluation is post-hoc only; nothing observes a running task

    def task_ended(self, snapshot: TaskSnapshot) -> None:
        failed = snapshot.status == "failed" or snapshot.unresolved
        if not self._jobs.service.wants(failed=failed):
            return
        source = trace_source(snapshot)
        self._jobs.queue.submit(f"post_hoc:{snapshot.task_id}", lambda: self._jobs.run(source))


class EvaluationJobs:
    """Runs one evaluation in the queue's own context."""

    def __init__(self, *, service: EvaluationService, queue: EvaluationQueue) -> None:
        self.service = service
        self.queue = queue
        self.recorded: list[uuid.UUID] = []

    async def run(self, source: TraceSource) -> uuid.UUID:
        evaluation_id = await self.service.evaluate(source)
        self.recorded.append(evaluation_id)
        del self.recorded[:-100]
        return evaluation_id


# ── assembly ───────────────────────────────────────────────────────────────


def build_evaluation(config: AppConfig, *, storage: StorageBackend) -> tuple[RulesObserver, EvaluationJobs] | None:
    """`None` when `evaluation.enabled` is false: no observer, no queue — the
    runtime runs exactly as without evaluation."""

    section = config.evaluation
    if not section.enabled:
        return None
    persist_timeout = config.agent.bounds.wall_clock_timeout_seconds + 120.0
    sink = StorageEvaluationSink(storage, persist_timeout_seconds=persist_timeout)
    service = EvaluationService(
        settings=EvaluationSettings(
            sample_successful=section.post_hoc.sample_successful,
            always_on_failure=section.post_hoc.always_on_failure,
            max_observation_chars=section.max_observation_chars,
        ),
        sink=sink,
    )
    jobs = EvaluationJobs(service=service, queue=EvaluationQueue(maxsize=section.queue_size))
    return RulesObserver(jobs=jobs), jobs


__all__ = [
    "EvaluationJobs",
    "RulesObserver",
    "StorageEvaluationSink",
    "build_evaluation",
    "trace_source",
]
