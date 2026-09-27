"""Creating, listing and cancelling reminders (docs/22 §1, 02 §9).

This module holds the scheduler's *rules* — what a valid reminder is, the
quota, provenance — and nothing about *authority*. It never decides whether a
principal may create, see or cancel a job: the caller (the composition root's
facade or the agent tool adapter) asks the authorization engine first and
calls in here only with an allowed request. It also never fires anything;
creating a job only records when it will come due.

Creation is split in two so the request lifecycle (02 §2) keeps its order —
rate precheck, then validation, then authorization, then the write:
`prepare()` does the first two, the caller authorizes, `commit()` writes.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Sequence

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.config.schema import SchedulerConfig
from server.scheduler.backend import SchedulerBackend
from server.scheduler.schedule import ScheduleError, as_utc, first_fire, parse_schedule
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.security.usage import SchedulerQuota
from server.storage.models import ReminderDelivery, ScheduledJob, ScheduledJobFiring
from shared.schemas.enums import AuditActor, AuditResult, JobStatus, Visibility
from shared.schemas.scheduler import FiringOutcome, LastFiring, ReasonSource, ScheduledJobView

TRUNCATION_MARK = "…"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReminderRefused(Exception):
    """The reminder is invalid (`422`). `reason` is a stable identifier; the
    message is safe to show."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class NewReminder:
    """A reminder to create, with every identity field server-derived."""

    owner_user_id: uuid.UUID
    task_reason: str
    schedule: str
    reason_source: ReasonSource
    graph_id: uuid.UUID | None = None
    origin_task_id: uuid.UUID | None = None
    device_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None


@dataclass(frozen=True)
class PreparedReminder:
    request: NewReminder
    task_reason: str
    schedule: str
    first_fire: datetime


def normalize_reason(text: str, *, source: ReasonSource, max_chars: int) -> str:
    """`[LOCKED]` a non-empty reason (SCHED-001, DM-T4). A reason the user typed
    is refused when too long; one copied from a task instruction is cut at the
    bound (it is still a prefix of the user's own words, and the task input
    bound itself is the runtime's, not ours)."""

    if not isinstance(text, str):
        raise ReminderRefused("task_reason_required", "a reminder needs a reason")
    cleaned = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ch >= " ").strip()
    if not cleaned:
        raise ReminderRefused("task_reason_required", "a reminder needs a reason (SCHED-001)")
    if len(cleaned) > max_chars:
        if source is ReasonSource.USER:
            raise ReminderRefused("task_reason_too_long", f"the reason is longer than {max_chars} characters")
        cleaned = cleaned[: max_chars - len(TRUNCATION_MARK)].rstrip() + TRUNCATION_MARK
    return cleaned


class SchedulerService:
    def __init__(
        self,
        *,
        config: SchedulerConfig,
        backend: SchedulerBackend,
        quota: SchedulerQuota,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._config = config
        self._backend = backend
        self._quota = quota
        self._clock = clock

    @property
    def config(self) -> SchedulerConfig:
        return self._config

    @property
    def backend(self) -> SchedulerBackend:
        return self._backend

    def now(self) -> datetime:
        return as_utc(self._clock())

    # ── creation ────────────────────────────────────────────────────────

    async def precheck_quota(self, session: AsyncSession, *, user_id: uuid.UUID) -> None:
        """`LimitExceeded` (→ `429`) if the user is at their scheduler quota."""

        await self._quota.precheck_creation(session, user_id=user_id, now=self.now())

    def prepare(self, request: NewReminder) -> PreparedReminder:
        reason = normalize_reason(
            request.task_reason, source=request.reason_source, max_chars=self._config.max_task_reason_chars
        )
        try:
            schedule = parse_schedule(request.schedule)
            first = first_fire(
                schedule,
                now=self.now(),
                min_recurrence=timedelta(minutes=self._config.min_recurrence_minutes),
                max_horizon=timedelta(days=self._config.max_horizon_days),
            )
        except ScheduleError as exc:
            raise ReminderRefused(exc.reason, str(exc)) from None
        return PreparedReminder(request=request, task_reason=reason, schedule=schedule.raw, first_fire=first)

    async def commit(
        self, session: AsyncSession, prepared: PreparedReminder, *, audit: AuditLogger, actor: AuditActor
    ) -> ScheduledJob:
        request = prepared.request
        job = ScheduledJob(
            job_id=uuid.uuid4(),
            owner_user_id=request.owner_user_id,
            source_user_id=request.owner_user_id,
            graph_id=request.graph_id,
            visibility=Visibility.PRIVATE,
            task_reason=prepared.task_reason,
            schedule=prepared.schedule,
            created_at=self.now(),
            reason_source=request.reason_source.value,
            origin_task_id=request.origin_task_id,
            created_by_device_id=request.device_id,
        )
        await self._backend.add(session, job, first_fire=prepared.first_fire)
        await audit.record(
            actor=actor, action=AuditAction.SCHEDULER_JOB_CREATED, resource=f"scheduledjob:{job.job_id}",
            result=AuditResult.SUCCESS, user_id=request.owner_user_id, device_id=request.device_id,
            session_id=request.session_id, graph_id=request.graph_id,
        )
        return job

    # ── reading ─────────────────────────────────────────────────────────

    async def candidates(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        graph_ids: frozenset[uuid.UUID],
        include_inactive: bool,
        limit: int,
        cursor: str | None = None,
    ) -> tuple[list[ScheduledJob], str | None]:
        """The caller's own jobs and graph-visible jobs in graphs they belong
        to — a *query*, not a decision: the caller re-checks every row with the
        engine's `readable()` predicate before showing it (RAUTH-004)."""

        visible = ScheduledJob.owner_user_id == user_id
        if graph_ids:
            visible = or_(
                visible,
                and_(ScheduledJob.visibility == Visibility.GRAPH, ScheduledJob.graph_id.in_(graph_ids)),
            )
        query = select(ScheduledJob).where(visible)
        if not include_inactive:
            query = query.where(ScheduledJob.status == JobStatus.ACTIVE)
        position = _decode_cursor(cursor)
        if position is not None:
            created, job_id = position
            query = query.where(
                or_(
                    ScheduledJob.created_at < created,
                    and_(ScheduledJob.created_at == created, ScheduledJob.job_id < job_id),
                )
            )
        query = query.order_by(ScheduledJob.created_at.desc(), ScheduledJob.job_id.desc()).limit(limit + 1)
        rows = list((await session.execute(query)).scalars())
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = _encode_cursor(as_utc(last.created_at), last.job_id)
        return rows, next_cursor

    async def last_firings(
        self, session: AsyncSession, job_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, ScheduledJobFiring]:
        if not job_ids:
            return {}
        rows = (
            await session.execute(
                select(ScheduledJobFiring)
                .where(ScheduledJobFiring.job_id.in_(list(job_ids)))
                .order_by(ScheduledJobFiring.fired_at)
            )
        ).scalars()
        latest: dict[uuid.UUID, ScheduledJobFiring] = {}
        for row in rows:
            latest[row.job_id] = row
        return latest

    @staticmethod
    def view(job: ScheduledJob, last: ScheduledJobFiring | None = None) -> ScheduledJobView:
        return ScheduledJobView(
            job_id=job.job_id,
            owner_user_id=job.owner_user_id,
            source_user_id=job.source_user_id,
            graph_id=job.graph_id,
            visibility=job.visibility,
            task_reason=job.task_reason,
            schedule=job.schedule,
            status=job.status,
            created_at=as_utc(job.created_at),
            next_fire_at=as_utc(job.next_fire_at) if job.next_fire_at is not None else None,
            reason_source=ReasonSource(job.reason_source),
            origin_task_id=job.origin_task_id,
            last_firing=(
                LastFiring(
                    scheduled_for=as_utc(last.scheduled_for),
                    fired_at=as_utc(last.fired_at),
                    outcome=FiringOutcome(last.outcome),
                    late=last.late,
                    coalesced=last.coalesced,
                )
                if last is not None
                else None
            ),
        )

    # ── cancellation ────────────────────────────────────────────────────

    async def cancel(
        self,
        session: AsyncSession,
        job: ScheduledJob,
        *,
        audit: AuditLogger,
        actor: AuditActor,
        device_id: uuid.UUID | None = None,
        session_id: uuid.UUID | None = None,
    ) -> bool:
        """Stop the job for good and withdraw any reminder of it still waiting
        for a device — including a one-time job that already fired but whose
        reminder is still queued for an offline device. Idempotent: a cancelled
        job is left as it is."""

        if job.status is JobStatus.CANCELLED:
            return False
        await self._backend.mark(session, job, next_fire_at=None, status=JobStatus.CANCELLED)
        await drop_undelivered(session, job_id=job.job_id)
        await audit.record(
            actor=actor, action=AuditAction.SCHEDULER_JOB_CANCELLED, resource=f"scheduledjob:{job.job_id}",
            result=AuditResult.SUCCESS, user_id=job.owner_user_id, device_id=device_id,
            session_id=session_id, graph_id=job.graph_id,
        )
        return True


async def drop_undelivered(
    session: AsyncSession, *, job_id: uuid.UUID | None = None, device_id: uuid.UUID | None = None
) -> int:
    """Withdraw reminders not yet acknowledged — for a cancelled job, or a
    device that may no longer receive them."""

    query = update(ReminderDelivery).where(ReminderDelivery.status.in_(("pending", "sent")))
    if job_id is not None:
        query = query.where(ReminderDelivery.job_id == job_id)
    if device_id is not None:
        query = query.where(ReminderDelivery.device_id == device_id)
    result = await session.execute(query.values(status="dropped"))
    await session.flush()
    return int(result.rowcount or 0)


def _encode_cursor(created_at: datetime, job_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}|{job_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[datetime, uuid.UUID] | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created, _, job = base64.urlsafe_b64decode(padded.encode()).decode().partition("|")
        return as_utc(datetime.fromisoformat(created)), uuid.UUID(job)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise ReminderRefused("cursor_invalid", "the pagination cursor is not valid") from None


__all__ = [
    "NewReminder",
    "PreparedReminder",
    "ReminderRefused",
    "SchedulerService",
    "drop_undelivered",
    "normalize_reason",
]
