"""What the HTTP layer and the device channel need from the scheduler (docs/22).

`server.gateway` sits below `server.scheduler` in the layering (16 §2), so it
declares these Protocols and the composition root supplies the implementations
(`server/composition/scheduler.py`) on `app.state.scheduler` and
`app.state.reminders` — the same pattern as `memory_port.py`.

Every method takes the authenticated `Principal` (or, on the device channel,
the socket's server-verified `(user_id, device_id)` binding); none accepts an
owner, a visibility, or a device from a request body as authority.
Implementations raise `AppError` in 02 §1.7's vocabulary, including `503
dependency_unavailable` with `scheduler` (02 §13, FAIL-010).
"""

from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from server.security.audit import AuditLogger
from shared.schemas.authorization import Principal
from shared.schemas.scheduler import JobCreateRequest, JobListResponse, ScheduledJobView


class SchedulerPort(Protocol):
    async def create(
        self, session: AsyncSession, *, principal: Principal, body: JobCreateRequest, audit: AuditLogger
    ) -> ScheduledJobView: ...

    async def recall(
        self, session: AsyncSession, *, principal: Principal, job_id: uuid.UUID, audit: AuditLogger
    ) -> ScheduledJobView:
        """One job, if the engine lets the caller read it (an idempotent replay)."""
        ...

    async def list(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        limit: int,
        cursor: str | None,
        include_inactive: bool,
        audit: AuditLogger,
    ) -> JobListResponse: ...

    async def cancel(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        job_id: uuid.UUID,
        confirmation_token: str | None,
        audit: AuditLogger,
    ) -> None: ...


class ReminderInbox(Protocol):
    """The device channel's half of reminder delivery (docs/22 §3, docs/23 §4).
    Called only with the socket's server-verified binding."""

    async def device_connected(self, *, user_id: uuid.UUID, device_id: uuid.UUID) -> None:
        """Send this device the reminders still owed to it."""
        ...

    async def acknowledge(self, *, user_id: uuid.UUID, device_id: uuid.UUID, delivery_id: uuid.UUID) -> bool:
        """The device showed the reminder. Only a delivery addressed to exactly
        this device of this user can be acknowledged."""
        ...


__all__ = ["ReminderInbox", "SchedulerPort"]
