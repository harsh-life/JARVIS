"""What the operator control routes need from the supervisor (18 §5.4).

`server.gateway` sits below `server.agent` and `server.composition` in the
layering (16 §2), so it cannot import the runtime or the control service. It
declares this Protocol; the composition root supplies the implementation on
`app.state.supervisor_control` (`server/composition/supervisor.py`).

Every method takes a `SuperuserPrincipal` — the output of `get_superuser` — and
nothing else can stand in for one: there is no user, session, device, model or
tool identity anywhere in this interface.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from server.gateway.superuser_auth import SuperuserPrincipal
from server.security.audit import AuditLogger


class ControlScope(str, Enum):
    TASK = "task"
    USER = "user"
    DEVICE = "device"


class ControlTargetNotFound(Exception):
    """A task-scope stop named a task that does not exist."""


@dataclass
class StopReport:
    """Which tasks a stop reached, by what it did to each (`StopOutcome`)."""

    stopped: list[uuid.UUID] = field(default_factory=list)
    signalled: list[uuid.UUID] = field(default_factory=list)
    already_terminal: list[uuid.UUID] = field(default_factory=list)


@dataclass
class LatchReport:
    latched: bool
    changed: bool
    tasks: StopReport = field(default_factory=StopReport)


@dataclass(frozen=True)
class BreakGlassView:
    """A break-glass record as the operator sees it (20 §2.2)."""

    record_id: str
    task_id: uuid.UUID
    user_id: uuid.UUID
    executables: list[str]
    max_invocations: int
    remaining: int
    reason: str
    activated_at: datetime
    expires_at: datetime


class BreakGlassRefusalKind(str, Enum):
    CONFLICT = "conflict"        # the server or the task is not in a state that allows it
    VALIDATION = "validation"    # the request asks for something it may not
    NOT_FOUND = "not_found"


class BreakGlassRequestRefused(Exception):
    def __init__(self, kind: BreakGlassRefusalKind, code: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code


class SupervisorControlPort(Protocol):
    async def stop(
        self, session: AsyncSession, audit: AuditLogger, *, principal: SuperuserPrincipal,
        scope: ControlScope, target_id: uuid.UUID, reason: str,
    ) -> StopReport: ...

    async def global_stop(
        self, session: AsyncSession, audit: AuditLogger, *, principal: SuperuserPrincipal, reason: str,
    ) -> LatchReport: ...

    async def global_clear(
        self, session: AsyncSession, audit: AuditLogger, *, principal: SuperuserPrincipal,
    ) -> LatchReport: ...

    async def activate_break_glass(
        self, session: AsyncSession, audit: AuditLogger, *, principal: SuperuserPrincipal,
        task_id: uuid.UUID, user_id: uuid.UUID, executables: list[str], max_invocations: int,
        expires_in_seconds: int | None, reason: str,
    ) -> BreakGlassView: ...

    async def revoke_break_glass(
        self, session: AsyncSession, audit: AuditLogger, *, principal: SuperuserPrincipal,
        task_id: uuid.UUID, reason: str,
    ) -> BreakGlassView: ...

    async def list_break_glass(
        self, session: AsyncSession, audit: AuditLogger, *, principal: SuperuserPrincipal,
    ) -> list[BreakGlassView]: ...
