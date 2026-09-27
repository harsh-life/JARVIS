"""Firing and delivery — docs/22 §2/§3.

```
job due → re-check (deterministic): owner active? job still active? if graph-
  scoped, owner still an active member of the graph?
    any "no" → job cancelled, firing recorded `cancelled_recheck`, audited,
               nothing delivered
  → deliver a reminder to the OWNER's own active devices
       connected → sent now; offline → queued until reconnect (reminders carry
       no authority, so they may wait) + a content-free wake
  → audit fired / delivered / queued / undeliverable
```

**There is no principal at fire time.** Nothing here calls the runtime,
activates a capability, touches a tool, a device operation, a confirmation or
a grant — and nothing here *can*: the package's imports are storage, the audit
writer, and the three Protocols below, and `pyproject.toml`'s contracts forbid
the rest. What a reminder does is exactly one thing: put the owner's own words
on the owner's own screen. Anything after that is the user starting a task.

**Idempotent.** One firing row per `(job_id, scheduled_for)` (a unique index):
a duplicate scheduler event, two runners, or a crash-and-replay cannot fire the
same occurrence twice, and a delivery row per `(firing, device)` cannot be sent
as two different reminders.

**Misfires** (the server was down at the due time, docs/22 §3): occurrences
older than `misfire_grace_minutes` are recorded as one `missed` firing (with
how many were folded into it) and audited — reported through `GET /jobs`
(`last_firing`), never silently dropped. The latest occurrence still within
grace is delivered late, flagged `late`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol, Sequence

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.config.schema import SchedulerConfig
from server.scheduler.backend import SchedulerBackend
from server.scheduler.schedule import ScheduleError, as_utc, parse_schedule
from server.scheduler.service import drop_undelivered
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.storage import StorageBackend
from server.storage.models import ReminderDelivery, ScheduledJob, ScheduledJobFiring
from shared.schemas.device_channel import DeviceReminder, DeviceWakePush
from shared.schemas.enums import AuditActor, AuditResult, JobStatus
from shared.schemas.scheduler import FiringOutcome

logger = logging.getLogger("hypermind.scheduler.firing")

# A reminder delivered within this of its due time is on time, not "late".
ON_TIME_TOLERANCE = timedelta(minutes=2)
# How many past occurrences of a recurring job one misfire inspects.
_MAX_OCCURRENCES = 1000
_DUE_BATCH = 100


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── what the scheduler needs from outside (satisfied by the composition root) ──


class FireTimeChecks(Protocol):
    """The deterministic re-checks, answered from live state (never cached)."""

    async def user_active(self, session: AsyncSession, user_id: uuid.UUID) -> bool: ...

    async def active_member(self, session: AsyncSession, *, graph_id: uuid.UUID, user_id: uuid.UUID) -> bool: ...

    async def active_devices(self, session: AsyncSession, user_id: uuid.UUID) -> Sequence[uuid.UUID]:
        """The user's own devices that are not revoked."""
        ...

    async def device_belongs_to(self, session: AsyncSession, *, device_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """The device is `user_id`'s and not revoked."""
        ...


class ReminderChannel(Protocol):
    """Puts one typed reminder on one device's authenticated socket. It takes a
    `DeviceReminder` and nothing else — no operation can travel this way."""

    async def push(self, reminder: DeviceReminder, *, user_id: uuid.UUID) -> bool: ...


class WakeSender(Protocol):
    """Asks a sleeping device to reconnect (docs/23 §4). It is given the device
    and its owner (so the waker can refuse any other user's device) and nothing
    else, so no reminder content can reach a third-party push service; the
    payload it may send is `DeviceWakePush` — `{"type":"wake"}`."""

    async def wake(self, *, device_id: uuid.UUID, user_id: uuid.UUID) -> None: ...


class NoChannel:
    """No device channel (Android disabled): every reminder waits, then expires."""

    async def push(self, reminder: DeviceReminder, *, user_id: uuid.UUID) -> bool:
        return False


class NoWake:
    """No push wake configured (`android.push.provider: none`): nothing is sent
    to any third party."""

    async def wake(self, *, device_id: uuid.UUID, user_id: uuid.UUID) -> None:
        return None


class PayloadWake:
    """A `WakeSender` over any push transport `send(device_id, payload)`. The
    payload is fixed here — the one content-free wake — so a transport never
    sees anything else (SCH-T4, ANDC-T9)."""

    PAYLOAD = DeviceWakePush().model_dump_json()

    def __init__(self, send: Callable[[uuid.UUID, str], "object"]) -> None:
        self._send = send

    async def wake(self, *, device_id: uuid.UUID, user_id: uuid.UUID) -> None:
        result = self._send(device_id, self.PAYLOAD)
        if asyncio.iscoroutine(result):
            await result


# ── the firer ─────────────────────────────────────────────────────────────


class ReminderFirer:
    def __init__(
        self,
        *,
        storage: StorageBackend,
        backend: SchedulerBackend,
        config: SchedulerConfig,
        checks: FireTimeChecks,
        channel: ReminderChannel | None = None,
        wake: WakeSender | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._storage = storage
        self._backend = backend
        self._config = config
        self._checks = checks
        self._channel = channel or NoChannel()
        self.wake_sender = wake or NoWake()
        # Replaceable so tests (and a future simulated clock) control "now".
        self.clock = clock

    def now(self) -> datetime:
        return as_utc(self.clock())

    async def tick(self, now: datetime | None = None) -> int:
        """One pass: expire stale queued reminders, then fire everything due.
        Returns how many jobs were handled."""

        now = as_utc(now or self.now())
        await self.expire_deliveries(now)
        async with self._storage.session() as session:
            due = [job.job_id for job in await self._backend.list_due(session, now=now, limit=_DUE_BATCH)]
        handled = 0
        for job_id in due:
            try:
                if await self.fire(job_id, now=now):
                    handled += 1
            except Exception:  # noqa: BLE001 — one bad job never stops the others
                logger.exception("firing job %s failed; it stays due and is retried", job_id)
        return handled

    async def next_due_at(self) -> datetime | None:
        async with self._storage.session() as session:
            return await self._backend.next_due_at(session)

    async def fire(self, job_id: uuid.UUID, *, now: datetime | None = None) -> bool:
        """Handle one due job. Returns False if there was nothing to do (not due,
        not active, or this occurrence was already handled)."""

        now = as_utc(now or self.now())
        async with self._storage.session() as session:
            try:
                handled, firing_id = await self._fire_in(session, job_id, now)
            except IntegrityError:
                # This occurrence was already fired (a duplicate event, or a
                # concurrent runner): the unique (job_id, scheduled_for) holds.
                await session.rollback()
                return False
        if firing_id is not None:
            await self.deliver_firing(firing_id)
        return handled

    async def _fire_in(
        self, session: AsyncSession, job_id: uuid.UUID, now: datetime
    ) -> tuple[bool, uuid.UUID | None]:
        firing_id: uuid.UUID | None = None
        audit = AuditLogger(session, request_id=uuid.uuid4())
        job = await session.get(ScheduledJob, job_id)
        if (
            job is None
            or job.status is not JobStatus.ACTIVE
            or job.next_fire_at is None
            or as_utc(job.next_fire_at) > now
        ):
            return False, None
        due_at = as_utc(job.next_fire_at)

        try:
            schedule = parse_schedule(job.schedule)
        except ScheduleError:
            # Only reachable if a stored row was altered outside the API.
            await self._refuse(session, audit, job, due_at, now, reason="schedule_invalid", coalesced=1)
            await session.commit()
            return True, None

        occurrences = (
            schedule.occurrences_between(due_at, now, limit=_MAX_OCCURRENCES)
            if schedule.recurring else [due_at]
        ) or [due_at]
        grace = timedelta(minutes=self._config.misfire_grace_minutes)

        refusal = await self._recheck(session, job)
        if refusal is not None:
            await self._refuse(session, audit, job, occurrences[-1], now, reason=refusal,
                               coalesced=len(occurrences))
            await session.commit()
            return True, None

        latest = occurrences[-1]
        deliver_at = latest if now - latest <= grace else None
        missed = occurrences[:-1] if deliver_at is not None else occurrences
        if missed:
            session.add(ScheduledJobFiring(
                job_id=job.job_id, owner_user_id=job.owner_user_id, scheduled_for=missed[-1], fired_at=now,
                outcome=FiringOutcome.MISSED.value, late=True, coalesced=len(missed),
            ))
            await self._audit(audit, job, AuditAction.SCHEDULER_JOB_MISSED, AuditResult.FAILURE,
                              f"scheduledjob:{job.job_id}")

        if deliver_at is not None:
            firing = ScheduledJobFiring(
                firing_id=uuid.uuid4(), job_id=job.job_id, owner_user_id=job.owner_user_id, scheduled_for=deliver_at, fired_at=now,
                outcome=FiringOutcome.QUEUED.value, late=now - deliver_at > ON_TIME_TOLERANCE, coalesced=1,
            )
            session.add(firing)
            devices = list(await self._checks.active_devices(session, job.owner_user_id))
            if not devices:
                firing.outcome = FiringOutcome.UNDELIVERABLE.value
                await self._audit(audit, job, AuditAction.SCHEDULER_REMINDER_UNDELIVERABLE,
                                  AuditResult.FAILURE, f"scheduledjob:{job.job_id}")
            else:
                expires = now + timedelta(hours=self._config.pending_delivery_ttl_hours)
                for device_id in devices:
                    session.add(ReminderDelivery(
                        firing_id=firing.firing_id, job_id=job.job_id, user_id=job.owner_user_id,
                        device_id=device_id, status="pending", created_at=now, expires_at=expires,
                    ))
            await self._audit(audit, job, AuditAction.SCHEDULER_JOB_FIRED, AuditResult.SUCCESS,
                              f"scheduledjob:{job.job_id}")
            firing_id = firing.firing_id if devices else None

        following = schedule.next_after(now) if schedule.recurring else None
        await self._backend.mark(
            session, job, next_fire_at=following, status=None if following is not None else JobStatus.FIRED
        )
        await session.commit()
        return True, firing_id

    async def _recheck(self, session: AsyncSession, job: ScheduledJob) -> str | None:
        if not await self._checks.user_active(session, job.owner_user_id):
            return "user_not_active"
        if job.graph_id is not None and not await self._checks.active_member(
            session, graph_id=job.graph_id, user_id=job.owner_user_id
        ):
            return "membership_revoked"
        return None

    async def _refuse(self, session: AsyncSession, audit: AuditLogger, job: ScheduledJob, scheduled_for: datetime,
                      now: datetime, *, reason: str, coalesced: int) -> None:
        session.add(ScheduledJobFiring(
            job_id=job.job_id, owner_user_id=job.owner_user_id, scheduled_for=scheduled_for, fired_at=now,
            outcome=FiringOutcome.CANCELLED_RECHECK.value, reason=reason, late=False, coalesced=coalesced,
        ))
        await self._backend.mark(session, job, next_fire_at=None, status=JobStatus.CANCELLED)
        await drop_undelivered(session, job_id=job.job_id)
        await self._audit(audit, job, AuditAction.SCHEDULER_JOB_RECHECK_FAILED, AuditResult.BLOCKED,
                          f"scheduledjob:{job.job_id}:{reason}")

    @staticmethod
    async def _audit(audit: AuditLogger, job: ScheduledJob, action: AuditAction, result: AuditResult,
                     resource: str, device_id: uuid.UUID | None = None) -> None:
        await audit.record(
            actor=AuditActor.SYSTEM, action=action, resource=resource, result=result,
            user_id=job.owner_user_id, graph_id=job.graph_id, device_id=device_id,
        )

    # ── delivery ────────────────────────────────────────────────────────

    async def deliver_firing(self, firing_id: uuid.UUID) -> None:
        async with self._storage.session() as session:
            rows = (await session.execute(
                select(ReminderDelivery.delivery_id).where(
                    ReminderDelivery.firing_id == firing_id, ReminderDelivery.status == "pending")
            )).scalars().all()
        for delivery_id in rows:
            await self._send(delivery_id, wake_if_offline=True)
        await self._settle_firing(firing_id)

    async def device_connected(self, *, user_id: uuid.UUID, device_id: uuid.UUID) -> int:
        """Send every reminder still owed to this device of this user —
        pending ones and ones sent but never acknowledged (at-least-once; the
        device de-duplicates by `delivery_id`)."""

        async with self._storage.session() as session:
            rows = (await session.execute(
                select(ReminderDelivery.delivery_id, ReminderDelivery.firing_id)
                .where(
                    ReminderDelivery.device_id == device_id,
                    ReminderDelivery.user_id == user_id,
                    ReminderDelivery.status.in_(("pending", "sent")),
                )
                .order_by(ReminderDelivery.created_at)
            )).all()
        sent = 0
        firings: set[uuid.UUID] = set()
        for delivery_id, firing_id in rows:
            if await self._send(delivery_id, wake_if_offline=False):
                sent += 1
            firings.add(firing_id)
        for firing_id in firings:
            await self._settle_firing(firing_id)
        return sent

    async def acknowledge(self, *, user_id: uuid.UUID, device_id: uuid.UUID, delivery_id: uuid.UUID) -> bool:
        async with self._storage.session() as session:
            result = await session.execute(
                update(ReminderDelivery)
                .where(
                    ReminderDelivery.delivery_id == delivery_id,
                    ReminderDelivery.device_id == device_id,
                    ReminderDelivery.user_id == user_id,
                    ReminderDelivery.status.in_(("pending", "sent")),
                )
                .values(status="acked", acked_at=self.now())
            )
            if not result.rowcount:
                await session.rollback()
                return False
            audit = AuditLogger(session, request_id=uuid.uuid4())
            await audit.record(
                actor=AuditActor.USER, action=AuditAction.SCHEDULER_REMINDER_ACKNOWLEDGED,
                resource=f"reminder:{delivery_id}", result=AuditResult.SUCCESS, user_id=user_id, device_id=device_id,
            )
            await session.commit()
        return True

    async def _send(self, delivery_id: uuid.UUID, *, wake_if_offline: bool) -> bool:
        """Re-check, then put one reminder on its device's socket. The same
        re-checks as firing apply at send time, because a queued reminder may
        wait hours: a cancelled job, a revoked device, a suspended owner or a
        left graph each withdraw it instead."""

        async with self._storage.session() as session:
            audit = AuditLogger(session, request_id=uuid.uuid4())
            delivery = await session.get(ReminderDelivery, delivery_id)
            if delivery is None or delivery.status not in ("pending", "sent"):
                return False
            job = await session.get(ScheduledJob, delivery.job_id)
            firing = await session.get(ScheduledJobFiring, delivery.firing_id)
            now = self.now()
            withdrawn = None
            if job is None or firing is None or job.status is JobStatus.CANCELLED:
                withdrawn = "job_cancelled"
            elif delivery.user_id != job.owner_user_id:
                withdrawn = "not_owner"  # structurally impossible; checked anyway
            elif not await self._checks.device_belongs_to(session, device_id=delivery.device_id,
                                                          user_id=job.owner_user_id):
                withdrawn = "device_not_active"
            else:
                withdrawn = await self._recheck(session, job)
            if withdrawn is None and as_utc(delivery.expires_at) <= now:
                delivery.status = "expired"
                await self._audit(audit, job, AuditAction.SCHEDULER_REMINDER_EXPIRED, AuditResult.FAILURE,
                                  f"reminder:{delivery.delivery_id}", delivery.device_id)
                await session.commit()
                return False
            if withdrawn is not None:
                delivery.status = "dropped"
                await audit.record(
                    actor=AuditActor.SYSTEM, action=AuditAction.SCHEDULER_REMINDER_DROPPED,
                    resource=f"reminder:{delivery.delivery_id}:{withdrawn}", result=AuditResult.BLOCKED,
                    user_id=delivery.user_id, device_id=delivery.device_id,
                )
                await session.commit()
                return False

            reminder = DeviceReminder(
                delivery_id=delivery.delivery_id, job_id=job.job_id, device_id=delivery.device_id,
                task_reason=job.task_reason, scheduled_for=as_utc(firing.scheduled_for),
                late=firing.late or (now - as_utc(firing.scheduled_for) > ON_TIME_TOLERANCE),
                recurring=_is_recurring(job.schedule),
            )
            first_attempt = delivery.status == "pending" and delivery.sent_at is None
            # The owner's id travels with it: the channel sends only to a
            # socket bound to exactly this user and device.
            pushed = await self._channel.push(reminder, user_id=job.owner_user_id)
            if pushed:
                delivery.status = "sent"
                delivery.sent_at = now
                await self._audit(audit, job, AuditAction.SCHEDULER_REMINDER_DELIVERED, AuditResult.SUCCESS,
                                  f"reminder:{delivery.delivery_id}", delivery.device_id)
            elif first_attempt and wake_if_offline:
                await self._audit(audit, job, AuditAction.SCHEDULER_REMINDER_QUEUED, AuditResult.SUCCESS,
                                  f"reminder:{delivery.delivery_id}", delivery.device_id)
            await session.commit()
            device_id, owner = delivery.device_id, delivery.user_id
        if not pushed and wake_if_offline:
            try:
                await self.wake_sender.wake(device_id=device_id, user_id=owner)
            except Exception:  # noqa: BLE001 — a failed wake leaves the reminder queued
                logger.warning("wake for device %s failed", device_id)
        return pushed

    async def _settle_firing(self, firing_id: uuid.UUID) -> None:
        """A firing is `delivered` once every device it owed has it."""

        async with self._storage.session() as session:
            firing = await session.get(ScheduledJobFiring, firing_id)
            if firing is None or firing.outcome not in (FiringOutcome.QUEUED.value, FiringOutcome.DELIVERED.value):
                return
            statuses = set((await session.execute(
                select(ReminderDelivery.status).where(ReminderDelivery.firing_id == firing_id)
            )).scalars())
            outstanding = statuses & {"pending"}
            outcome = FiringOutcome.QUEUED.value if outstanding else FiringOutcome.DELIVERED.value
            if statuses <= {"dropped", "expired"}:
                outcome = FiringOutcome.UNDELIVERABLE.value
            if firing.outcome != outcome:
                firing.outcome = outcome
                await session.commit()

    async def expire_deliveries(self, now: datetime | None = None) -> int:
        now = as_utc(now or self.now())
        async with self._storage.session() as session:
            stale = (await session.execute(
                select(ReminderDelivery).where(
                    ReminderDelivery.status.in_(("pending", "sent")), ReminderDelivery.expires_at <= now)
            )).scalars().all()
            if not stale:
                return 0
            audit = AuditLogger(session, request_id=uuid.uuid4())
            firings = set()
            for delivery in stale:
                delivery.status = "expired"
                firings.add(delivery.firing_id)
                await audit.record(
                    actor=AuditActor.SYSTEM, action=AuditAction.SCHEDULER_REMINDER_EXPIRED,
                    resource=f"reminder:{delivery.delivery_id}", result=AuditResult.FAILURE,
                    user_id=delivery.user_id, device_id=delivery.device_id,
                )
            await session.commit()
        for firing_id in firings:
            await self._settle_firing(firing_id)
        return len(stale)


def _is_recurring(schedule: str) -> bool:
    try:
        return parse_schedule(schedule).recurring
    except ScheduleError:
        return False


# ── the runner (docs/22 §3) ───────────────────────────────────────────────


class SchedulerRunner:
    """Drives `ReminderFirer.tick` for the app's lifetime. The first tick runs
    at startup, which is where a restart's misfires are found and handled.
    `poke()` wakes it early (a job created for two minutes from now should not
    wait out the poll interval)."""

    def __init__(self, firer: ReminderFirer, *, poll_seconds: float) -> None:
        self._firer = firer
        self._poll = poll_seconds
        self._event = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def firer(self) -> ReminderFirer:
        return self._firer

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="scheduler-runner")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def poke(self) -> None:
        self._event.set()

    async def _loop(self) -> None:
        while True:
            try:
                await self._firer.tick()
                next_due = await self._firer.next_due_at()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the runner outlives any one failure
                logger.exception("scheduler tick failed")
                next_due = None
            wait = self._poll
            if next_due is not None:
                wait = max(0.05, min(wait, (next_due - self._firer.now()).total_seconds()))
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass


__all__ = [
    "FireTimeChecks",
    "NoChannel",
    "NoWake",
    "PayloadWake",
    "ReminderChannel",
    "ReminderFirer",
    "SchedulerRunner",
    "WakeSender",
]
