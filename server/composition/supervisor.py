"""Operator stop and the global emergency latch (18 §5.4).

The one place that stops tasks on the **operator's** authority, and the one
place that sets or clears the global latch. Every entry takes a
`SuperuserPrincipal` — produced only by `server.gateway.superuser_auth`, which
nothing below the gateway can import (pyproject: "Only the gateway reaches
superuser authority"). The runtime gets a read-only view of the latch
(`SupervisorGate`, the `SupervisorGatePort` it declares) and no way to change it.

Two rules shape every method here:

* **In memory first, database second.** A task running in another request
  holds the store's write lock until it stops (SQLite is single-writer). So a
  stop trips every live target, and a global stop sets the in-process latch,
  *before* this request touches the database; the stopped task aborts its
  in-flight call, commits, and releases the lock this request then takes.
* **Fail closed.** The latch lives in two places — this process's memory and
  the `supervisor_latch` row — and a submission is refused if *either* says
  latched, or if the row cannot be read. Setting writes memory first; clearing
  writes the row first and memory only after that succeeded. A crash, a failed
  write, or a restart can therefore leave the latch set, but never clear it.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.agent import AgentRuntime, StopOutcome
from server.agent.breaker import TripSource
from server.composition.facade import AgentTaskFacade
from server.gateway.control_port import ControlScope, ControlTargetNotFound, LatchReport, StopReport
from server.gateway.superuser_auth import SuperuserPrincipal
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.composition.latch import LATCH_ID, InProcessLatch
from server.storage.models import AgentTask, SupervisorLatch
from shared.schemas.agent import AgentTaskStatus, TERMINAL_STATUSES
from shared.schemas.enums import AuditActor, AuditResult

# The same identifier rule the breaker enforces: a reason is recorded in the
# audit trail, which carries no free-form text.
REASON_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
_REASON = re.compile(REASON_PATTERN)
_NON_TERMINAL = [s.value for s in AgentTaskStatus if s not in TERMINAL_STATUSES]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SupervisorControl:
    """Implements `server.gateway.control_port.SupervisorControlPort`."""

    def __init__(self, *, runtime: AgentRuntime, facade: AgentTaskFacade, latch: InProcessLatch) -> None:
        self._runtime = runtime
        self._facade = facade
        self._latch = latch

    # ── operator stop ───────────────────────────────────────────────────

    async def stop(
        self, session: AsyncSession, audit: AuditLogger, *, principal: SuperuserPrincipal,
        scope: ControlScope, target_id: uuid.UUID, reason: str,
    ) -> StopReport:
        _require(principal)
        _require_reason(reason)
        source = TripSource.OPERATOR.value

        # 1. In memory: every matching live task is tripped before any await.
        signalled, to_enforce = self._trip_live(
            [s for s in self._runtime.states.live() if _matches(s, scope, target_id)],
            reason=reason, source=source,
        )

        # 2. Database: enforce what nobody else is driving, and close orphans.
        if scope is ControlScope.TASK:
            targets = [] if signalled else [target_id]
        else:
            column = AgentTask.user_id if scope is ControlScope.USER else AgentTask.device_id
            targets = _ordered(to_enforce, await _non_terminal(session, column == target_id), skip=signalled)
        report = await self._enforce(session, audit, targets, reason=reason, source=source)
        report.signalled[:0] = signalled

        resource = f"control:stop:{scope.value}:{target_id}:{reason}"
        if scope is ControlScope.TASK and not (report.stopped or report.signalled or report.already_terminal):
            await _control_audit(audit, AuditAction.CONTROL_STOP, resource, AuditResult.FAILURE)
            raise ControlTargetNotFound()
        await _control_audit(audit, AuditAction.CONTROL_STOP, resource, AuditResult.SUCCESS)
        return report

    # ── the global latch ────────────────────────────────────────────────

    async def global_stop(
        self, session: AsyncSession, audit: AuditLogger, *, principal: SuperuserPrincipal, reason: str,
    ) -> LatchReport:
        _require(principal)
        _require_reason(reason)
        source = TripSource.GLOBAL_LATCH.value

        # 1. In memory, without yielding: refuse new tasks, then trip every live
        #    one. `AgentRuntime.submit` re-checks this flag right after
        #    registering a task, so a task being created now is either swept
        #    here or stopped there.
        self._latch.latched = True
        signalled, to_enforce = self._trip_live(self._runtime.states.live(), reason=reason, source=source)

        # 2. Persist the latch, then stop everything the sweep could not reach.
        row = await session.get(SupervisorLatch, LATCH_ID)
        was_latched = row is not None and row.latched
        if row is None:
            session.add(SupervisorLatch(latch_id=LATCH_ID, latched=True, reason=reason,
                                        changed_at=_utcnow(), changed_by=principal.token_fingerprint))
        elif not row.latched:
            row.latched, row.reason = True, reason
            row.changed_at, row.changed_by = _utcnow(), principal.token_fingerprint
        await session.flush()

        targets = _ordered(to_enforce, await _non_terminal(session), skip=signalled)
        report = await self._enforce(session, audit, targets, reason=reason, source=source)
        report.signalled[:0] = signalled

        if not was_latched:
            await _control_audit(audit, AuditAction.BREAKER_GLOBAL_LATCHED, f"breaker:global:{reason}",
                                 AuditResult.SUCCESS)
        outcome = "already_latched" if was_latched else "latched"
        await _control_audit(audit, AuditAction.CONTROL_GLOBAL_STOP,
                             f"control:global_stop:{reason}:{outcome}", AuditResult.SUCCESS)
        return LatchReport(latched=True, changed=not was_latched, tasks=report)

    async def global_clear(
        self, session: AsyncSession, audit: AuditLogger, *, principal: SuperuserPrincipal,
    ) -> LatchReport:
        _require(principal)

        row = await session.get(SupervisorLatch, LATCH_ID)
        was_latched = self._latch.latched or (row is not None and row.latched)
        if row is None:
            if was_latched:  # set in memory, never persisted: record the clear
                session.add(SupervisorLatch(latch_id=LATCH_ID, latched=False, reason=None,
                                            changed_at=_utcnow(), changed_by=principal.token_fingerprint))
        elif row.latched:
            row.latched, row.reason = False, None
            row.changed_at, row.changed_by = _utcnow(), principal.token_fingerprint
        # The row first: if this write fails, the process stays latched.
        await session.flush()
        self._latch.latched = False

        if was_latched:
            await _control_audit(audit, AuditAction.BREAKER_GLOBAL_CLEARED, "breaker:global", AuditResult.SUCCESS)
        outcome = "cleared" if was_latched else "already_clear"
        await _control_audit(audit, AuditAction.CONTROL_GLOBAL_CLEAR, f"control:global_clear:{outcome}",
                             AuditResult.SUCCESS)
        return LatchReport(latched=False, changed=was_latched)

    # ── shared ──────────────────────────────────────────────────────────

    def _trip_live(self, states, *, reason: str, source: str) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
        """Trip every given live task — synchronously, no `await` — and sort
        them: a task some request is driving (not paused) is *signalled*, and
        that request enforces the stop at its next checkpoint; a paused one has
        no driver and is enforced here, in the database pass."""

        signalled, to_enforce = [], []
        for state in states:
            self._runtime.signal_stop(state.task_id, reason=reason, source=source)
            (to_enforce if state.pending is not None else signalled).append(state.task_id)
        return signalled, to_enforce

    async def _enforce(
        self, session: AsyncSession, audit: AuditLogger, targets: list[uuid.UUID], *, reason: str, source: str,
    ) -> StopReport:
        env = self._facade.environment(session, audit)
        report = StopReport()
        for task_id in targets:
            outcome = await self._runtime.operator_stop(env, task_id, reason=reason, source=source)
            if outcome is StopOutcome.STOPPED:
                report.stopped.append(task_id)
            elif outcome is StopOutcome.SIGNALLED:
                report.signalled.append(task_id)
            elif outcome is StopOutcome.ALREADY_TERMINAL:
                report.already_terminal.append(task_id)
        return report


def _require(principal: SuperuserPrincipal) -> None:
    """Defence in depth behind the route's `get_superuser` dependency: only a
    verified grant reaches any control method."""

    if not isinstance(principal, SuperuserPrincipal) or not principal.grant.is_valid():
        raise PermissionError("superuser authority required")


def _require_reason(reason: str) -> None:
    if not _REASON.fullmatch(reason or ""):
        raise ValueError("reason must be a short identifier")


def _matches(state, scope: ControlScope, target_id: uuid.UUID) -> bool:
    if scope is ControlScope.TASK:
        return state.task_id == target_id
    if scope is ControlScope.USER:
        return state.principal.user_id == target_id
    return state.principal.device_id == target_id


async def _non_terminal(session: AsyncSession, *where) -> list[uuid.UUID]:
    query = select(AgentTask.task_id).where(AgentTask.status.in_(_NON_TERMINAL), *where)
    return list((await session.execute(query)).scalars().all())


def _ordered(first: list[uuid.UUID], more: list[uuid.UUID], *, skip: list[uuid.UUID]) -> list[uuid.UUID]:
    skipped = set(skip)
    return [t for t in dict.fromkeys([*first, *more]) if t not in skipped]


async def _control_audit(audit: AuditLogger, action: AuditAction, resource: str, result: AuditResult) -> None:
    await audit.record(actor=AuditActor.SUPERUSER, action=action, resource=resource, result=result)
