"""The scheduler, joined to the deterministic security layer (docs/22 §1).

`server.scheduler` may not import the authorization engine, and the gateway may
not import `server.scheduler` (16 §2). This module is where they meet; it adds
no authorization of its own — every decision below is
`AuthorizationEngine.authorize` or the engine's `readable()` predicate.

* `SchedulerFacade` is `SchedulerPort` (02 §9): rate precheck → validate →
  authorize → write → audit, in 02 §2's order.
* `ReminderToolAdapter` is the agent's path: `scheduler.create.create_reminder`
  inside an `execute` task. The runtime has already authorized the operation
  (capability, mode ceiling, D1); the adapter takes the reminder's reason from
  `ToolInvocation.task_input` — the task's own user input, bound by the runtime
  — and refuses any `task_reason` the worker tries to supply.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.capabilities.registry import CREATE_REMINDER_OPERATION, SCHEDULER_CREATE_CAPABILITY
from server.execution.device_hub import DeviceHub
from server.gateway.errors import AppError
from server.gateway.security import SecurityCore
from server.graph.authorization import AccessRequest, AuthorizationOutcome
from server.graph.ports import ResourceDescriptor
from server.graph.predicate import readable
from server.scheduler.firing import ReminderFirer
from server.scheduler.service import NewReminder, ReminderRefused, SchedulerService
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.security.usage import LimitExceeded
from server.storage.models import Device, ScheduledJob, User
from server.tools.registry import ToolDefinition
from shared.schemas.agent import ExecutionPlatform, OperationSpec, ToolInvocation, ToolOutput
from shared.schemas.agent_config import ToolContract
from shared.schemas.authorization import DenialSurface, Operation, Principal, ResourceType
from shared.schemas.device_channel import DeviceReminder
from shared.schemas.enums import AuditActor, AuditResult, RiskCategory, UserStatus
from shared.schemas.errors import ErrorCode
from shared.schemas.scheduler import JobCreateRequest, JobListResponse, ReasonSource, ScheduledJobView

logger = logging.getLogger("hypermind.composition.scheduler")

REMINDER_TOOL_ID = "scheduler.reminders"
_MAX_GRAPHS = 1000


def _refusal(outcome: AuthorizationOutcome) -> AppError:
    if outcome.surface is DenialSurface.PROHIBITED:
        return AppError(ErrorCode.PROHIBITED, "this action is never allowed")
    if outcome.surface is DenialSurface.FORBIDDEN:
        return AppError(ErrorCode.UNAUTHORIZED, "not permitted")
    return AppError(ErrorCode.NOT_FOUND, "not found")


def _rate_limited(exc: LimitExceeded) -> AppError:
    return AppError(
        ErrorCode.RATE_LIMITED,
        "reminder limit reached",
        details={"limit": exc.limit, "retry_after": exc.retry_after_seconds},
    )


def _descriptor(job: ScheduledJob) -> ResourceDescriptor:
    return ResourceDescriptor(
        resource_type=ResourceType.SCHEDULEDJOB, resource_ref=str(job.job_id),
        owner_user_id=job.owner_user_id, visibility=job.visibility, graph_id=job.graph_id,
        source_user_id=job.source_user_id,
    )


class SchedulerFacade:
    def __init__(self, *, service: SchedulerService, core: SecurityCore) -> None:
        self._service = service
        self._core = core

    @property
    def service(self) -> SchedulerService:
        return self._service

    async def _authorize(self, session: AsyncSession, request: AccessRequest, audit: AuditLogger) -> AuthorizationOutcome:
        return await self._core.engine.authorize(session, request, audit=audit)

    async def _limit_refused(self, audit: AuditLogger, principal: Principal, exc: LimitExceeded,
                             actor: AuditActor = AuditActor.USER) -> None:
        await audit.record(
            actor=actor, action=AuditAction.USAGE_LIMIT_EXCEEDED, resource=f"limit:{exc.limit}",
            result=AuditResult.BLOCKED, user_id=principal.user_id, device_id=principal.device_id,
            session_id=principal.session_id,
        )

    async def _refused(self, audit: AuditLogger, *, user_id: uuid.UUID, device_id: uuid.UUID | None,
                       session_id: uuid.UUID | None, reason: str, actor: AuditActor) -> None:
        await audit.record(
            actor=actor, action=AuditAction.SCHEDULER_JOB_REFUSED, resource=f"scheduledjob:refused:{reason}",
            result=AuditResult.BLOCKED, user_id=user_id, device_id=device_id, session_id=session_id,
        )

    # ── SchedulerPort (02 §9) ───────────────────────────────────────────

    async def create(
        self, session: AsyncSession, *, principal: Principal, body: JobCreateRequest, audit: AuditLogger
    ) -> ScheduledJobView:
        # 02 §2 step 4: the rate/quota precheck comes before anything else.
        try:
            await self._service.precheck_quota(session, user_id=principal.user_id)
        except LimitExceeded as exc:
            await self._limit_refused(audit, principal, exc)
            raise _rate_limited(exc) from None

        # Step 5: validate (SCHED-001's non-empty reason, the schedule's bounds).
        request = NewReminder(
            owner_user_id=principal.user_id, task_reason=body.task_reason, schedule=body.schedule,
            reason_source=ReasonSource.USER, graph_id=body.graph_id, device_id=principal.device_id,
            session_id=principal.session_id,
        )
        try:
            prepared = self._service.prepare(request)
        except ReminderRefused as exc:
            await self._refused(audit, user_id=principal.user_id, device_id=principal.device_id,
                                session_id=principal.session_id, reason=exc.reason, actor=AuditActor.USER)
            raise AppError(ErrorCode.VALIDATION_FAILED, str(exc), details={"reason": exc.reason}) from None

        # Step 6: authorize. `graph_id` is a claim: D1 checks the caller is an
        # active member; it grants nothing else (RAUTH V1).
        outcome = await self._authorize(session, AccessRequest(
            principal=principal, operation=Operation.CREATE, resource_type=ResourceType.SCHEDULEDJOB,
            graph_id=body.graph_id,
        ), audit)
        if not outcome.allowed:
            raise _refusal(outcome)

        job = await self._service.commit(session, prepared, audit=audit, actor=AuditActor.USER)
        return self._service.view(job)

    async def recall(
        self, session: AsyncSession, *, principal: Principal, job_id: uuid.UUID, audit: AuditLogger
    ) -> ScheduledJobView:
        outcome = await self._authorize(session, AccessRequest(
            principal=principal, operation=Operation.READ, resource_type=ResourceType.SCHEDULEDJOB,
            resource_ref=str(job_id),
        ), audit)
        if not outcome.allowed:
            raise _refusal(outcome)
        job = await session.get(ScheduledJob, job_id)
        if job is None:
            raise AppError(ErrorCode.NOT_FOUND, "not found")
        last = await self._service.last_firings(session, [job.job_id])
        return self._service.view(job, last.get(job.job_id))

    async def list(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        limit: int,
        cursor: str | None,
        include_inactive: bool,
        audit: AuditLogger,
    ) -> JobListResponse:
        graphs = await self._core.graph_repository.graphs_for_user(
            session, user_id=principal.user_id, limit=_MAX_GRAPHS
        )
        member_of = frozenset(g.graph_id for g in graphs)
        try:
            rows, next_cursor = await self._service.candidates(
                session, user_id=principal.user_id, graph_ids=member_of,
                include_inactive=include_inactive, limit=limit, cursor=cursor,
            )
        except ReminderRefused as exc:
            raise AppError(ErrorCode.VALIDATION_FAILED, str(exc), details={"reason": exc.reason}) from None
        # RAUTH-004 re-check with live membership, whatever the query returned.
        visible = [
            job for job in rows
            if readable(user_id=principal.user_id, resource=_descriptor(job),
                        is_active_member_of_resource_graph=job.graph_id in member_of)
        ]
        if len(visible) != len(rows):
            logger.error("job query returned %d row(s) the principal may not read; dropped",
                         len(rows) - len(visible))
        last = await self._service.last_firings(session, [job.job_id for job in visible])
        return JobListResponse(
            items=[self._service.view(job, last.get(job.job_id)) for job in visible], next_cursor=next_cursor
        )

    async def cancel(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        job_id: uuid.UUID,
        confirmation_token: str | None,
        audit: AuditLogger,
    ) -> None:
        # D3 owner-only, D4 visibility (a job the caller cannot see is 404), and
        # the tier table's `consequential` for a delete — a single-use token
        # bound to exactly this cancel.
        outcome = await self._authorize(session, AccessRequest(
            principal=principal, operation=Operation.DELETE, resource_type=ResourceType.SCHEDULEDJOB,
            resource_ref=str(job_id), confirmation_token=confirmation_token,
        ), audit)
        if outcome.needs_confirmation:
            binding = outcome.confirmation_required_for
            assert binding is not None
            issued = await self._core.confirmations.issue(
                session, binding=binding, risk_category=outcome.risk_category
            )
            raise AppError(
                ErrorCode.CONFIRMATION_REQUIRED,
                "cancelling this reminder needs your confirmation",
                details={
                    "confirmation_token": issued.token,
                    "expires_at": issued.expires_at.isoformat(),
                    "action": "cancel_reminder",
                    "risk_category": outcome.risk_category.value,
                },
            )
        if not outcome.allowed:
            raise _refusal(outcome)
        job = await session.get(ScheduledJob, job_id)
        if job is None:
            raise AppError(ErrorCode.NOT_FOUND, "not found")
        await self._service.cancel(
            session, job, audit=audit, actor=AuditActor.USER, device_id=principal.device_id,
            session_id=principal.session_id,
        )


# ── the agent path (docs/22 §1) ─────────────────────────────────────────


@dataclass(frozen=True)
class _ReminderScope:
    session: AsyncSession
    audit: AuditLogger


# The request-scoped transaction a reminder tool call writes in. Installed by
# the agent facade around runtime calls (the same pattern as
# `CURRENT_SECRET_RESOLVER`): the pilot's SQLite has one writer, so the tool must
# write inside the runtime's own transaction, never open a second one.
CURRENT_REMINDER_SCOPE: ContextVar[_ReminderScope | None] = ContextVar("current_reminder_scope", default=None)


@contextlib.contextmanager
def reminder_scope(session: AsyncSession, audit: AuditLogger) -> Iterator[None]:
    token = CURRENT_REMINDER_SCOPE.set(_ReminderScope(session=session, audit=audit))
    try:
        yield
    finally:
        CURRENT_REMINDER_SCOPE.reset(token)


_ALLOWED_ARGUMENTS = frozenset({"schedule"})


class ReminderToolAdapter:
    """`scheduler.create.create_reminder`. Everything identity-shaped comes from
    the `ToolInvocation` the runtime built — the owner is the task's principal,
    the reason is the task's own input — never from the worker's arguments."""

    def __init__(self, service: SchedulerService) -> None:
        self._service = service

    async def execute(self, invocation: ToolInvocation) -> ToolOutput:
        if invocation.operation != CREATE_REMINDER_OPERATION:
            return ToolOutput(ok=False, error="unknown_operation")
        scope = CURRENT_REMINDER_SCOPE.get()
        if scope is None:
            return ToolOutput(ok=False, error="scheduler_unavailable")
        arguments = dict(invocation.arguments or {})
        if "task_reason" in arguments:
            # SCH-T1 agent half: the worker does not author the reason.
            return ToolOutput(
                ok=False, error="task_reason_is_bound",
                content="The reminder's reason is taken from the user's own request. Pass only `schedule`.",
            )
        unknown = set(arguments) - _ALLOWED_ARGUMENTS
        if unknown or not isinstance(arguments.get("schedule"), str):
            return ToolOutput(ok=False, error="invalid_arguments",
                              content="create_reminder takes exactly one argument: `schedule` (a string).")
        if invocation.task_input is None:
            # Fail closed: without the runtime's binding there is no user reason.
            return ToolOutput(ok=False, error="task_reason_unavailable")

        session, audit = scope.session, scope.audit
        user_id, device_id = invocation.user_id, invocation.device_id
        try:
            await self._service.precheck_quota(session, user_id=user_id)
        except LimitExceeded as exc:
            await audit.record(
                actor=AuditActor.AGENT, action=AuditAction.USAGE_LIMIT_EXCEEDED, resource=f"limit:{exc.limit}",
                result=AuditResult.BLOCKED, user_id=user_id, device_id=device_id,
            )
            return ToolOutput(ok=False, error=f"rate_limited:{exc.limit}")

        request = NewReminder(
            owner_user_id=user_id, task_reason=invocation.task_input, schedule=arguments["schedule"],
            reason_source=ReasonSource.TASK_INPUT, graph_id=None, origin_task_id=invocation.task_id,
            device_id=device_id,
        )
        try:
            prepared = self._service.prepare(request)
        except ReminderRefused as exc:
            await audit.record(
                actor=AuditActor.AGENT, action=AuditAction.SCHEDULER_JOB_REFUSED,
                resource=f"scheduledjob:refused:{exc.reason}", result=AuditResult.BLOCKED,
                user_id=user_id, device_id=device_id,
            )
            return ToolOutput(ok=False, error=exc.reason, content=str(exc))
        job = await self._service.commit(session, prepared, audit=audit, actor=AuditActor.AGENT)
        return ToolOutput(
            ok=True,
            content=(
                f"Reminder {job.job_id} set; first due {prepared.first_fire.isoformat()}. "
                "It will notify the user's own devices and will not do anything by itself."
            ),
        )


def reminder_tool_definition(service: SchedulerService) -> ToolDefinition:
    return ToolDefinition(
        contract=ToolContract(
            tool_id=REMINDER_TOOL_ID, version="1",
            description=(
                "Set a reminder for the user from their own request. Argument: `schedule` — an "
                "ISO-8601 datetime with a UTC offset, or a 5-field cron expression optionally "
                "prefixed `CRON_TZ=<zone> `. The reminder's text is the user's request itself."
            ),
            input_schema={
                "type": "object", "properties": {"schedule": {"type": "string"}},
                "required": ["schedule"], "additionalProperties": False,
            },
            output_schema={"type": "string"},
            required_capability=SCHEDULER_CREATE_CAPABILITY, filesystem={}, network={},
            risk_category=RiskCategory.LOW_WRITE, timeout_seconds=10, confirmation_required=False,
            failure_behavior="observation", audit="every invocation",
        ),
        operations={
            CREATE_REMINDER_OPERATION: OperationSpec(
                resource_type=ResourceType.SCHEDULEDJOB.value, resource_operation=Operation.CREATE.value,
            ),
        },
        adapters={ExecutionPlatform.SERVER: ReminderToolAdapter(service)},
        binds_task_input=True,
    )




# ── firing and delivery (docs/22 §2/§3) ────────────────────────────────


class SecurityCoreFireChecks:
    """`FireTimeChecks` from live state: the user row, the engine's own
    membership reader (the one D1 predicate), and the device registry."""

    def __init__(self, core: SecurityCore) -> None:
        self._core = core

    async def user_active(self, session: AsyncSession, user_id: uuid.UUID) -> bool:
        user = await session.get(User, user_id)
        return user is not None and user.status is UserStatus.ACTIVE

    async def active_member(self, session: AsyncSession, *, graph_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        return await self._core.graph_repository.active_role(session, graph_id=graph_id, user_id=user_id) is not None

    async def active_devices(self, session: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
        rows = await session.execute(
            select(Device.device_id).where(Device.user_id == user_id, Device.revoked.is_(False))
        )
        return list(rows.scalars())

    async def device_belongs_to(self, session: AsyncSession, *, device_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        device = await session.get(Device, device_id)
        return device is not None and device.user_id == user_id and not device.revoked


class HubReminderChannel:
    """`ReminderChannel` over the device hub's typed `send_reminder` — the
    scheduler never holds the hub itself, so it has no way to call `send()`."""

    def __init__(self, hub: DeviceHub) -> None:
        self._hub = hub

    async def push(self, reminder: DeviceReminder, *, user_id: uuid.UUID) -> bool:
        return await self._hub.send_reminder(reminder, user_id=user_id)


class ReminderInboxAdapter:
    """`ReminderInbox` for the device channel."""

    def __init__(self, firer: ReminderFirer) -> None:
        self._firer = firer

    @property
    def firer(self) -> ReminderFirer:
        return self._firer

    async def device_connected(self, *, user_id: uuid.UUID, device_id: uuid.UUID) -> None:
        await self._firer.device_connected(user_id=user_id, device_id=device_id)

    async def acknowledge(self, *, user_id: uuid.UUID, device_id: uuid.UUID, delivery_id: uuid.UUID) -> bool:
        return await self._firer.acknowledge(user_id=user_id, device_id=device_id, delivery_id=delivery_id)


__all__ = [
    "CURRENT_REMINDER_SCOPE",
    "REMINDER_TOOL_ID",
    "HubReminderChannel",
    "ReminderInboxAdapter",
    "ReminderToolAdapter",
    "SchedulerFacade",
    "SecurityCoreFireChecks",
    "reminder_scope",
    "reminder_tool_definition",
]
