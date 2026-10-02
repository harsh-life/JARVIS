"""The unattended trigger loop — docs/29 §15.5 (Phase 5).

OD-AF-2 (ratified 2026-10-02, docs/DECISION_REGISTER.md §2K; PRD §22 as
amended): an agent with an active, step-up-granted StandingDelegation runs
unattended within its compiled envelope (≤ low_write), outputs to its owner's
inbox only. **The scheduler itself still never executes** — this loop is the
Agent Factory's, run by the composition root; it reads each delegation's own
compiled schedule (`server/agents/triggers.py`) and imports nothing of the
scheduler but its pure schedule parser.

One pass (`tick`), per active delegation, in its own store transaction:

1. **Expiry** (docs/29 §15.4): an expired delegation ends (`expired`, the
   owner is told); three days before, the owner is told once.
2. **Freshness** (§15.2): the delegation, the agent, its verified spec, the
   owner and the graph, read fresh. Anything that no longer holds ends the
   delegation (`invalidated`, the owner is told) and runs nothing.
3. **Occurrence** (§15.5): the latest occurrence since the last claimed one
   runs if it is within the grace; earlier ones are coalesced (one notice,
   never a catch-up storm). The claim is a compare-and-set on the delegation
   row, so concurrent passes never run one occurrence twice; one run per
   occurrence is also unique in the store.
4. **Admission**: the global latch (18 §5.4), the day's run limit and the
   month's budget each skip the occurrence with a notice to the owner.
5. **The run**: the delegation's `DelegatedPrincipal` (no device, no session)
   runs the agent as an ordinary task through the runtime — Agent Gateway,
   envelope gate, unattended ceiling, the one engine, execution — re-checked
   at every step. Its result goes to the owner's inbox, as data.

Nothing here authorizes anything: every decision about what the run may do
is the engine's, on the owner's live grants, at the moment of each step.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from server.agent.agent_run import AgentRunBinding
from server.agents.providers.native import NativeRuntimeProvider
from server.agents.triggers import expiry_notice_due, local_day, plan_occurrences
from server.gateway.errors import AppError
from server.scheduler.schedule import parse_schedule
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.storage.models import AgentRunRow, StandingDelegationRow
from shared.schemas.agent import AgentTaskStatus
from shared.schemas.agent_factory import (
    AgentNotice,
    AgentRunContext,
    AgentStatus,
    DelegationStatus,
)
from shared.schemas.authorization import DelegatedPrincipal, Principal  # noqa: F401 — M-AG162
from shared.schemas.enums import AuditActor, AuditResult

if TYPE_CHECKING:
    from server.composition.agents import AgentFactory
    from server.composition.facade import AgentTaskFacade
    from server.storage import StorageBackend

logger = logging.getLogger("hypermind.agents.triggers")


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass
class TriggerReport:
    """What one pass did — ids and closed codes only."""

    started: list[uuid.UUID] = field(default_factory=list)
    notices: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    ended: list[tuple[uuid.UUID, str]] = field(default_factory=list)


class AgentTriggerLoop:
    def __init__(self, *, factory: "AgentFactory", tasks: "AgentTaskFacade", storage: "StorageBackend",
                 grace_minutes: int, interval_seconds: float) -> None:
        self._factory = factory
        self._service = factory.service
        self._tasks = tasks
        self._storage = storage
        self._grace = timedelta(minutes=grace_minutes)
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None

    # ── one pass ────────────────────────────────────────────────────────

    async def tick(self, now: datetime | None = None) -> TriggerReport:
        now = _utc(now or datetime.now(timezone.utc))
        report = TriggerReport()
        async with self._storage.session() as session:
            ids = [row.delegation_id for row in await self._service.active_delegations(session)]
        for delegation_id in ids:
            try:
                await self._process(delegation_id, now, report)
            except Exception:  # noqa: BLE001 — one delegation never stops the others
                logger.exception("unattended trigger failed for a delegation")
        return report

    async def _notify(self, session, audit: AuditLogger, row: StandingDelegationRow, notice: AgentNotice,
                      report: TriggerReport) -> None:
        await self._service.notify(session, agent_id=row.agent_id, owner_user_id=row.owner_user_id, notice=notice,
                                   delegation_id=row.delegation_id)
        report.notices.append((row.agent_id, notice.value))
        if notice not in (AgentNotice.DELEGATION_EXPIRING,):
            await audit.record(actor=AuditActor.SYSTEM, action=AuditAction.AGENT_UNATTENDED_SKIPPED,
                               resource=f"agentdelegation:{row.delegation_id}:{notice.value}",
                               result=AuditResult.BLOCKED, user_id=row.owner_user_id)

    async def _end(self, session, audit: AuditLogger, row: StandingDelegationRow, status: DelegationStatus,
                   reason: str, report: TriggerReport) -> None:
        if await self._service.end_delegation(session, row, status, reason):
            action = (AuditAction.AGENT_DELEGATION_EXPIRED if status is DelegationStatus.EXPIRED
                      else AuditAction.AGENT_DELEGATION_INVALIDATED)
            await audit.record(actor=AuditActor.SYSTEM, action=action,
                               resource=f"agentdelegation:{row.delegation_id}:{reason}"[:128],
                               result=AuditResult.SUCCESS, user_id=row.owner_user_id)
            report.ended.append((row.agent_id, reason))

    async def _process(self, delegation_id: uuid.UUID, now: datetime, report: TriggerReport) -> None:
        async with self._storage.session() as session:
            audit = AuditLogger(session, request_id=uuid.uuid4())
            row = await session.get(StandingDelegationRow, delegation_id, populate_existing=True)
            if row is None or row.status != DelegationStatus.ACTIVE.value:
                return
            # 1. expiry
            if now >= _utc(row.expires_at):
                await self._end(session, audit, row, DelegationStatus.EXPIRED, "expired", report)
                await session.commit()
                return
            if expiry_notice_due(expires_at=_utc(row.expires_at), now=now,
                                 noticed_at=_utc(row.expiry_notice_at) if row.expiry_notice_at else None):
                row.expiry_notice_at = now
                await self._notify(session, audit, row, AgentNotice.DELEGATION_EXPIRING, report)
            # 2. freshness — everything read again, from the store
            refused = await self._factory.delegation_refusal(session, delegation_id=row.delegation_id,
                                                             agent_id=row.agent_id, now=now)
            if refused is not None:
                await self._end(session, audit, row, DelegationStatus.INVALIDATED, refused, report)
                await session.commit()
                return
            # 3. the occurrence
            schedule = parse_schedule(f"CRON_TZ={row.timezone} {row.allowed_trigger_cron}")
            anchor = max(_utc(row.created_at), _utc(row.last_occurrence_at) if row.last_occurrence_at else
                         _utc(row.created_at))
            plan = plan_occurrences(next_after=schedule.next_after, anchor=anchor, now=now, grace=self._grace)
            if plan.claim_through is None:
                await session.commit()
                return
            if not await self._service.claim_occurrence(session, row.delegation_id, plan.claim_through):
                await session.rollback()   # another pass has it
                return
            if plan.run_at is None:
                await self._notify(session, audit, row, AgentNotice.RUN_MISSED, report)
                await session.commit()
                return
            if plan.missed:
                await self._notify(session, audit, row, AgentNotice.MISFIRE_COALESCED, report)
            # 4. admission
            env = self._tasks.environment(session, audit)
            if not await env.supervisor.submissions_open():
                await self._notify(session, audit, row, AgentNotice.BREAKER_STOPPED, report)
                await session.commit()
                return
            start, end = local_day(plan.run_at, row.timezone)
            today = (await session.execute(select(func.count()).select_from(AgentRunRow).where(
                AgentRunRow.delegation_id == row.delegation_id, AgentRunRow.occurrence_at >= start,
                AgentRunRow.occurrence_at < end))).scalar_one()
            if today >= row.max_runs_per_day:
                await self._notify(session, audit, row, AgentNotice.RUN_LIMIT_REACHED, report)
                await session.commit()
                return
            loaded = await self._service.load(session, row.agent_id, fresh=True)
            assert loaded is not None and loaded[1] is not None  # checked fresh above
            definition, spec = loaded
            month_budget = min(spec.budget.per_month, row.budget_per_month)
            spent = await self._service.month_spend(session, row.agent_id, now)
            if spent >= month_budget:
                await self._notify(session, audit, row, AgentNotice.BUDGET_EXHAUSTED, report)
                await session.commit()
                return
            # 5. the run
            await self._run(session, audit, row, definition, spec, plan.run_at,
                            budget=min(row.budget_per_run, month_budget - spent), report=report)

    async def _run(self, session, audit: AuditLogger, row: StandingDelegationRow, definition, spec,
                   occurrence: datetime, *, budget: float, report: TriggerReport) -> None:
        from server.composition.agents import _PresentUserRun  # the one native run port

        factory = self._factory
        profile = factory.registries.model_profiles.get(spec.selection.model_profile_id)
        runtime = factory.registries.runtimes.get(spec.selection.runtime_id)
        if (definition.status != AgentStatus.ACTIVE.value or profile is None or not profile.profile.enabled
                or profile.profile.version != spec.selection.model_profile_version
                or runtime is None or not runtime.enabled or spec.selection.runtime_id != "native"):
            await self._notify(session, audit, row, AgentNotice.RUN_NOT_STARTED, report)
            await session.commit()
            return
        run_id = uuid.uuid4()
        try:
            async with session.begin_nested():
                run = await self._service.create_run(session, spec=spec, run_id=run_id, delegation=row,
                                                     occurrence_at=occurrence)
        except IntegrityError:
            await session.rollback()   # this occurrence already has its run
            return
        deadline = factory.run_deadline(spec)
        issued = await factory.gateway.issue(session, run, deadline=deadline)
        await audit.record(actor=AuditActor.SYSTEM, action=AuditAction.AGENT_TOKEN_ISSUED,
                           resource=f"agentrun:{run_id}:model,tool", result=AuditResult.SUCCESS,
                           user_id=row.owner_user_id)
        principal = DelegatedPrincipal(user_id=row.owner_user_id, agent_id=row.agent_id,
                                       delegation_id=row.delegation_id, run_id=run_id, graph_id=row.graph_id)
        binding = AgentRunBinding.from_spec(spec, run_id=run_id, model_ref=profile.profile.model_ref,
                                            budget_per_run=budget, input_text=factory.run_input(spec, run_id),
                                            delegation_id=row.delegation_id)
        # H-1: the claim, the run record and its tokens are durable before
        # anything runs.
        await session.commit()
        port = _PresentUserRun(tasks=self._tasks, factory=factory, session=session, principal=principal,
                               audit=audit, binding=binding)
        provider = NativeRuntimeProvider(port)
        await provider.provision(spec)
        try:
            await provider.start_run(AgentRunContext(
                run_id=run_id, agent_id=spec.agent_id, version=spec.version, spec_hash=spec.spec_hash,
                input_text=binding.input_text, deadline=deadline, run_token=issued.tool,
                model_run_token=issued.model,
            ))
        except Exception as exc:  # noqa: BLE001 — closed below, never left open
            # No task was created (a limit, the latch set meanwhile — or a
            # fault): the run ends here, failed, its tokens revoked, and the
            # owner is told. Nothing is retried.
            if not isinstance(exc, AppError):
                logger.exception("an unattended run could not start")
            await session.rollback()
            await self._service.run_finished(session, run_id, status=AgentTaskStatus.FAILED,
                                             failure="not_started", cost=0.0)
            await factory.gateway.revoke_run(session, run_id, "not_started")
            await self._notify(session, audit, row, AgentNotice.RUN_NOT_STARTED, report)
            await session.commit()
            return
        report.started.append(run_id)
        await session.commit()

    # ── restarts (docs/29 §25.2) ────────────────────────────────────────

    async def reconcile(self) -> list[uuid.UUID]:
        """At start, no unattended run can be live in this process. One that
        never got its task (the process died between claiming and starting)
        is closed `failed: interrupted`, its tokens revoked; one whose task
        ended is closed from it. Nothing is replayed: its occurrence stays
        claimed."""

        closed: list[uuid.UUID] = []
        async with self._storage.session() as session:
            rows = (await session.execute(select(AgentRunRow).where(
                AgentRunRow.kind == "unattended", AgentRunRow.finished_at.is_(None)))).scalars().all()
            for run in rows:
                if run.task_id is None:
                    await self._service.run_finished(session, run.run_id, status=AgentTaskStatus.FAILED,
                                                     failure="interrupted", cost=0.0, revoke_reason="interrupted")
                    closed.append(run.run_id)
                else:
                    synced = await self._service.get_run(session, run.run_id)
                    if synced is not None and synced.finished_at is not None:
                        await self._factory.gateway.revoke_run(session, run.run_id, "interrupted")
                        closed.append(run.run_id)
            await session.commit()
        return closed

    # ── the lifespan service ────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is None:
            await self.reconcile()
            self._task = asyncio.create_task(self._loop(), name="agent-trigger-loop")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the loop outlives any one failure
                logger.exception("unattended trigger pass failed")
            await asyncio.sleep(self._interval)


__all__ = ["AgentTriggerLoop", "TriggerReport"]
