"""SCH-T6 — jobs survive a restart; misfires are delivered late or reported
`missed`, never silently lost (docs/22 §3). Plus the audit trail's shape.

"Restart" here is real as far as the scheduler can tell: the storage backend is
disposed, a new one is opened on the same database file, and a brand-new
firer / the app's own lifespan runner picks the jobs up from the rows alone.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from server.composition.scheduler import SecurityCoreFireChecks
from server.scheduler.backend import DatabaseSchedulerBackend
from server.scheduler.firing import ReminderFirer
from server.security.events import AuditAction
from server.storage import SQLAlchemyStorageBackend
from server.storage.models import AuditEvent, ReminderDelivery, ScheduledJob, ScheduledJobFiring
from shared.schemas.enums import JobStatus
from tests.scheduler.test_firing import ANDROID, JOBS, create, next_frame, phone

pytestmark = pytest.mark.asyncio

GRACE = timedelta(minutes=60)


async def set_due(h, job_id: str, due: datetime) -> None:
    async with h.storage.session() as s:
        await s.execute(update(ScheduledJob).where(ScheduledJob.job_id == uuid.UUID(job_id))
                        .values(next_fire_at=due.astimezone(timezone.utc)))
        await s.commit()


def restarted_firer(h, storage: SQLAlchemyStorageBackend, now: datetime) -> ReminderFirer:
    return ReminderFirer(
        storage=storage, backend=DatabaseSchedulerBackend(), config=h.config.scheduler,
        checks=SecurityCoreFireChecks(h.core), clock=lambda: now,
    )


async def test_sch_t6_a_restart_finds_every_job_in_the_database(make_harness):
    h = await make_harness()
    alice = await h.user("alice")
    job = await create(h, alice, "renew the passport")
    [row] = await h.rows(ScheduledJob)
    due = row.next_fire_at.replace(tzinfo=timezone.utc)
    db_url = h.storage.engine.url.render_as_string(hide_password=False)
    await h.storage.dispose()

    # A new backend on the same file — nothing carried over in memory.
    storage = SQLAlchemyStorageBackend(db_url)
    try:
        firer = restarted_firer(h, storage, due + timedelta(seconds=30))
        assert await firer.next_due_at() == due
        assert await firer.tick() == 1
        async with storage.session() as s:
            [firing] = (await s.execute(ScheduledJobFiring.__table__.select())).all()
            assert firing.outcome in ("queued", "delivered") and firing.late is False
            assert firing.job_id == uuid.UUID(job["job_id"])
    finally:
        await storage.dispose()


async def test_within_grace_a_misfire_is_delivered_late(make_harness):
    h = await make_harness(config=ANDROID)
    alice = await h.user("alice")
    ws = await phone(h, alice)
    job = await create(h, alice, "call the landlord")
    now = datetime.now(timezone.utc)
    await set_due(h, job["job_id"], now - timedelta(minutes=40))  # the server was down

    firer = h.app.state.reminders.firer
    firer.clock = lambda: now
    assert await firer.tick() == 1
    frame = await next_frame(ws)
    assert frame is not None and frame["late"] is True and frame["task_reason"] == "call the landlord"
    [firing] = await h.rows(ScheduledJobFiring)
    assert firing.late is True and firing.outcome == "delivered"
    await ws.disconnect()


async def test_beyond_grace_a_misfire_is_recorded_missed_and_reported(make_harness):
    h = await make_harness(config=ANDROID)
    alice = await h.user("alice")
    ws = await phone(h, alice)
    job = await create(h, alice, "pick up the parcel")
    now = datetime.now(timezone.utc)
    await set_due(h, job["job_id"], now - GRACE - timedelta(minutes=5))

    firer = h.app.state.reminders.firer
    firer.clock = lambda: now
    assert await firer.tick() == 1
    assert await next_frame(ws, timeout=0.3) is None  # nothing delivered hours late
    assert await h.rows(ReminderDelivery) == []
    [firing] = await h.rows(ScheduledJobFiring)
    assert (firing.outcome, firing.late, firing.coalesced) == ("missed", True, 1)
    [row] = await h.rows(ScheduledJob)
    assert row.status is JobStatus.FIRED and row.next_fire_at is None
    [event] = await h.rows(AuditEvent, AuditEvent.action == AuditAction.SCHEDULER_JOB_MISSED.value)
    assert event.resource == f"scheduledjob:{job['job_id']}"

    # Reported to the owner, never silently dropped.
    listed = (await h.client.get(JOBS, params={"include_inactive": "true"}, headers=alice.auth)).json()
    [item] = listed["items"]
    assert item["last_firing"]["outcome"] == "missed"
    await ws.disconnect()


async def test_a_recurring_job_folds_missed_occurrences_and_resumes(make_harness):
    h = await make_harness(config=ANDROID)
    alice = await h.user("alice")
    ws = await phone(h, alice)
    resp = await h.client.post(JOBS, json={"task_reason": "drink water", "schedule": "0 * * * *"},
                               headers=alice.auth)
    job = resp.json()
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    top_of_hour = now.replace(minute=0)
    # Down for five hours: occurrences at -5h … -1h and the current hour.
    await set_due(h, job["job_id"], top_of_hour - timedelta(hours=5))
    firer = h.app.state.reminders.firer
    firer.clock = lambda: now

    assert await firer.tick() == 1
    firings = sorted(await h.rows(ScheduledJobFiring), key=lambda f: f.scheduled_for)
    missed = [f for f in firings if f.outcome == "missed"]
    delivered = [f for f in firings if f.outcome != "missed"]
    # The latest occurrence (within grace) is delivered; the rest are one row.
    assert len(delivered) == 1
    assert delivered[0].scheduled_for.replace(tzinfo=timezone.utc) == top_of_hour
    assert len(missed) == 1 and missed[0].coalesced == 5
    frame = await next_frame(ws)
    assert frame is not None and frame["recurring"] is True
    [row] = await h.rows(ScheduledJob)
    assert row.status is JobStatus.ACTIVE
    assert row.next_fire_at.replace(tzinfo=timezone.utc) == top_of_hour + timedelta(hours=1)
    await ws.disconnect()


async def test_a_zero_grace_reports_every_late_occurrence_as_missed(make_harness):
    h = await make_harness(config={"scheduler": {"misfire_grace_minutes": 0}})
    alice = await h.user("alice")
    job = await create(h, alice)
    now = datetime.now(timezone.utc)
    await set_due(h, job["job_id"], now - timedelta(minutes=1))
    firer = h.app.state.reminders.firer
    firer.clock = lambda: now
    await firer.tick()
    [firing] = await h.rows(ScheduledJobFiring)
    assert firing.outcome == "missed"


async def test_the_lifespan_runner_catches_up_after_a_restart(make_harness):
    """The app's own runner (a lifespan service) fires what came due while the
    server was down — started by the app, stopped with it."""

    h = await make_harness()
    alice = await h.user("alice")
    job = await create(h, alice, "overdue while down")
    await set_due(h, job["job_id"], datetime.now(timezone.utc) - timedelta(minutes=10))

    async with h.app.router.lifespan_context(h.app):
        for _ in range(200):
            if await h.rows(ScheduledJobFiring):
                break
            await asyncio.sleep(0.05)
    [firing] = await h.rows(ScheduledJobFiring)
    assert firing.late is True and firing.outcome in ("queued", "delivered")


async def test_one_failing_job_does_not_stop_the_others(make_harness, monkeypatch):
    h = await make_harness()
    alice = await h.user("alice")
    bad, good = await create(h, alice, "bad"), await create(h, alice, "good")
    now = datetime.now(timezone.utc)
    for job in (bad, good):
        await set_due(h, job["job_id"], now - timedelta(minutes=1))
    firer = h.app.state.reminders.firer
    firer.clock = lambda: now
    real = firer._fire_in

    async def flaky(session, job_id, when):
        if job_id == uuid.UUID(bad["job_id"]):
            raise RuntimeError("boom")
        return await real(session, job_id, when)

    monkeypatch.setattr(firer, "_fire_in", flaky)
    assert await firer.tick() == 1
    [firing] = await h.rows(ScheduledJobFiring)
    assert firing.job_id == uuid.UUID(good["job_id"])
    # The failed one is still due and retried next tick.
    [row] = await h.rows(ScheduledJob, ScheduledJob.job_id == uuid.UUID(bad["job_id"]))
    assert row.status is JobStatus.ACTIVE and row.next_fire_at is not None


# ── the audit trail ───────────────────────────────────────────────────────


async def test_every_scheduler_event_is_audited_without_the_reminders_words(make_harness):
    """Creation, refusal, firing, delivery, acknowledgement, miss, expiry,
    re-check failure and cancellation all leave an audit row naming ids — the
    reminder's text appears in none of them (SECRET-004 spirit; the text is the
    owner's private data)."""

    h = await make_harness(config={**ANDROID, "scheduler": {"pending_delivery_ttl_hours": 1}})
    alice = await h.user("alice")
    secret_words = "tell Sam about the surprise party on Friday"
    firer = h.app.state.reminders.firer

    ws = await phone(h, alice)
    delivered = await create(h, alice, secret_words)
    now = datetime.now(timezone.utc)
    await set_due(h, delivered["job_id"], now)
    firer.clock = lambda: now
    await firer.tick()
    frame = await next_frame(ws)
    await ws.send_json({"type": "reminder_ack", "delivery_id": frame["delivery_id"]})
    await asyncio.sleep(0.1)
    await ws.disconnect()

    missed = await create(h, alice, secret_words)
    await set_due(h, missed["job_id"], now - timedelta(hours=3))
    queued = await create(h, alice, secret_words)
    await set_due(h, queued["job_id"], now)
    await firer.tick()
    await firer.tick(now=now + timedelta(hours=2))  # the queued one expires

    await h.client.post(JOBS, json={"task_reason": " ", "schedule": "0 9 * * *"}, headers=alice.auth)
    cancelled = await create(h, alice, secret_words)
    first = await h.client.delete(f"{JOBS}/{cancelled['job_id']}", headers=alice.auth)
    token = first.json()["error"]["details"]["confirmation_token"]
    await h.client.delete(f"{JOBS}/{cancelled['job_id']}", headers={**alice.auth, "X-Confirmation-Token": token})

    events = await h.rows(AuditEvent)
    actions = {e.action for e in events}
    for expected in (
        AuditAction.SCHEDULER_JOB_CREATED, AuditAction.SCHEDULER_JOB_REFUSED, AuditAction.SCHEDULER_JOB_FIRED,
        AuditAction.SCHEDULER_REMINDER_DELIVERED, AuditAction.SCHEDULER_REMINDER_ACKNOWLEDGED,
        AuditAction.SCHEDULER_JOB_MISSED, AuditAction.SCHEDULER_REMINDER_QUEUED,
        AuditAction.SCHEDULER_REMINDER_EXPIRED, AuditAction.SCHEDULER_JOB_CANCELLED,
    ):
        assert expected.value in actions, expected
    for event in events:
        assert "Sam" not in event.resource and "surprise" not in event.resource
        assert len(event.resource) <= 128
