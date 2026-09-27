"""`SchedulerBackend` — docs/22 §3's small, replaceable interface.

The backend knows *when* jobs are due. It does not know what a reminder says,
who may see it, or where it goes; that is `service`/`firing`. Replacing it (a
different store, a real APScheduler instance, a distributed queue) touches only
this module.

`DatabaseSchedulerBackend` (config `apscheduler_db`) keeps its entire state on
the job row (`scheduled_jobs.next_fire_at`) in the application database — the
one job store, so jobs survive a restart with nothing to reconcile and there is
no second database or ad-hoc file. APScheduler's own `SQLAlchemyJobStore` is
deliberately not used: it pickles callables into its own table on a separate
synchronous engine, which would be a deserialization surface and a second
source of truth for the same jobs.

Every method takes the caller's session and never commits: the caller's
transaction decides what becomes durable, together with its audit rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.scheduler.schedule import as_utc
from server.storage.models import ScheduledJob
from shared.schemas.enums import JobStatus


class SchedulerBackend(Protocol):
    async def add(self, session: AsyncSession, job: ScheduledJob, *, first_fire: datetime) -> None:
        """Start tracking `job`, first due at `first_fire`."""
        ...

    async def remove(self, session: AsyncSession, job_id: uuid.UUID) -> bool:
        """Stop tracking the job: it will not come due again. Whether it did."""
        ...

    async def list(
        self, session: AsyncSession, *, owner_user_id: uuid.UUID, include_inactive: bool, limit: int
    ) -> Sequence[ScheduledJob]:
        """One owner's jobs, newest first."""
        ...

    async def list_due(self, session: AsyncSession, *, now: datetime, limit: int) -> Sequence[ScheduledJob]:
        """Active jobs whose next due instant is at or before `now`, oldest first."""
        ...

    async def mark(
        self,
        session: AsyncSession,
        job: ScheduledJob,
        *,
        next_fire_at: datetime | None,
        status: JobStatus | None = None,
    ) -> None:
        """Advance a job after one of its occurrences was handled: its next due
        instant (`None` = never again) and, optionally, its status."""
        ...

    async def next_due_at(self, session: AsyncSession) -> datetime | None:
        """The earliest instant any active job is due, for the runner's sleep."""
        ...


class DatabaseSchedulerBackend:
    name = "apscheduler_db"

    async def add(self, session: AsyncSession, job: ScheduledJob, *, first_fire: datetime) -> None:
        job.next_fire_at = as_utc(first_fire)
        job.status = JobStatus.ACTIVE
        session.add(job)
        await session.flush()

    async def remove(self, session: AsyncSession, job_id: uuid.UUID) -> bool:
        job = await session.get(ScheduledJob, job_id)
        if job is None or job.next_fire_at is None:
            return False
        job.next_fire_at = None
        await session.flush()
        return True

    async def list(
        self, session: AsyncSession, *, owner_user_id: uuid.UUID, include_inactive: bool, limit: int
    ) -> Sequence[ScheduledJob]:
        query = select(ScheduledJob).where(ScheduledJob.owner_user_id == owner_user_id)
        if not include_inactive:
            query = query.where(ScheduledJob.status == JobStatus.ACTIVE)
        query = query.order_by(ScheduledJob.created_at.desc()).limit(max(1, limit))
        return list((await session.execute(query)).scalars())

    async def list_due(self, session: AsyncSession, *, now: datetime, limit: int) -> Sequence[ScheduledJob]:
        query = (
            select(ScheduledJob)
            .where(
                ScheduledJob.status == JobStatus.ACTIVE,
                ScheduledJob.next_fire_at.is_not(None),
                ScheduledJob.next_fire_at <= as_utc(now),
            )
            .order_by(ScheduledJob.next_fire_at)
            .limit(max(1, limit))
        )
        return list((await session.execute(query)).scalars())

    async def mark(
        self,
        session: AsyncSession,
        job: ScheduledJob,
        *,
        next_fire_at: datetime | None,
        status: JobStatus | None = None,
    ) -> None:
        job.next_fire_at = as_utc(next_fire_at) if next_fire_at is not None else None
        if status is not None:
            job.status = status
        await session.flush()

    async def next_due_at(self, session: AsyncSession) -> datetime | None:
        query = (
            select(ScheduledJob.next_fire_at)
            .where(ScheduledJob.status == JobStatus.ACTIVE, ScheduledJob.next_fire_at.is_not(None))
            .order_by(ScheduledJob.next_fire_at)
            .limit(1)
        )
        value = (await session.execute(query)).scalar_one_or_none()
        return as_utc(value) if value is not None else None


def build_backend(name: str) -> SchedulerBackend:
    if name == DatabaseSchedulerBackend.name:
        return DatabaseSchedulerBackend()
    raise ValueError(f"unknown scheduler backend {name!r}")


__all__ = ["DatabaseSchedulerBackend", "SchedulerBackend", "build_backend"]
