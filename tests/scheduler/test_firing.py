"""docs/22 §2/§3 — firing and delivery, end to end through the production
composition root and the real device channel endpoint.

The rule under test: **a firing reminder delivers a message; it never
executes.** What it delivers goes only to the owner's own devices, carries no
authority, may wait for an offline device, and is withdrawn when the owner, the
job, the device or the graph membership stops being valid.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from server.auth.device import build_device_proof
from server.execution.android import MAPPING_VERSION
from server.gateway.app import API_V1_PREFIX
from server.scheduler.firing import PayloadWake
from server.security.events import AuditAction
from server.storage.models import (
    AgentTask,
    AuditEvent,
    CapabilityGrant,
    ConfirmationToken,
    Device,
    ReminderDelivery,
    ScheduledJob,
    ScheduledJobFiring,
    UsageEvent,
    User,
)
from shared.schemas.device_channel import DeviceReminder
from shared.schemas.enums import JobStatus, UserStatus, Visibility
from tests.device_channel_support import AsgiWebSocket, ChannelClosed

pytestmark = pytest.mark.asyncio

JOBS = f"{API_V1_PREFIX}/jobs"
ANDROID = {"android": {"enabled": True}}
REMINDER_KEYS = {"type", "delivery_id", "job_id", "device_id", "task_reason", "scheduled_for", "late", "recurring"}


# ── helpers ───────────────────────────────────────────────────────────────


async def phone(h, actor, *, features=("reminders",)) -> AsgiWebSocket:
    ws = AsgiWebSocket(h.app)
    await ws.connect()
    await ws.send_json({
        "type": "hello", "access_token": actor.token,
        "device_proof": build_device_proof(device_id=actor.device_id, device_credential=actor.credential),
        "mapping_version": MAPPING_VERSION, "client_version": "test", "features": list(features),
    })
    ack = await ws.receive_json()
    assert ack["type"] == "hello_ok", ack
    return ws


async def next_frame(ws: AsgiWebSocket, timeout: float = 2.0) -> dict | None:
    try:
        return await ws.receive_json(timeout=timeout)
    except asyncio.TimeoutError:
        return None


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0.01)


def firer(h):
    return h.app.state.reminders.firer


async def create(h, actor, reason="Take the medication", minutes=30, **extra) -> dict:
    when = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).replace(microsecond=0)
    resp = await h.client.post(JOBS, json={"task_reason": reason, "schedule": when.isoformat(), **extra},
                               headers=actor.auth)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def due_of(h, job_id: str) -> datetime:
    [job] = await h.rows(ScheduledJob, ScheduledJob.job_id == uuid.UUID(job_id))
    return job.next_fire_at.replace(tzinfo=timezone.utc)


async def fire_at(h, job_id: str, *, after: timedelta = timedelta(seconds=5)) -> datetime:
    now = await due_of(h, job_id) + after
    firer(h).clock = lambda: now
    assert await firer(h).fire(uuid.UUID(job_id), now=now)
    return now


class WakeLog:
    def __init__(self) -> None:
        self.sent: list[tuple[uuid.UUID, str]] = []

    def __call__(self, device_id: uuid.UUID, payload: str) -> None:
        self.sent.append((device_id, payload))


# ── delivery (SCH-T4) ─────────────────────────────────────────────────────


async def test_sch_t4_only_the_owners_devices_receive_it(make_harness):
    h = await make_harness(config=ANDROID)
    alice, bob = await h.user("alice"), await h.user("bob")
    graph = await h.shared_graph(alice, bob)
    job = await create(h, alice, "Submit the group report", graph_id=str(graph))
    # Even graph-visible, the reminder is the owner's private message.
    async with h.storage.session() as s:
        await s.execute(update(ScheduledJob).values(visibility=Visibility.GRAPH))
        await s.commit()
    alice_ws, bob_ws = await phone(h, alice), await phone(h, bob)

    await fire_at(h, job["job_id"])
    frame = await next_frame(alice_ws)
    assert frame is not None and frame["type"] == "reminder"
    assert set(frame) == REMINDER_KEYS  # a message, with no field a device could execute
    assert frame["task_reason"] == "Submit the group report"
    assert frame["device_id"] == str(alice.device_id) and frame["job_id"] == job["job_id"]
    assert frame["late"] is False
    assert await next_frame(bob_ws, timeout=0.3) is None

    deliveries = await h.rows(ReminderDelivery)
    assert {d.device_id for d in deliveries} == {alice.device_id}
    assert all(d.user_id == alice.user_id for d in deliveries)
    [firing] = await h.rows(ScheduledJobFiring)
    assert firing.outcome == "delivered"
    [row] = await h.rows(ScheduledJob)
    assert row.status is JobStatus.FIRED and row.next_fire_at is None
    for action in (AuditAction.SCHEDULER_JOB_FIRED, AuditAction.SCHEDULER_REMINDER_DELIVERED):
        [event] = await h.rows(AuditEvent, AuditEvent.action == action.value)
        assert "group report" not in event.resource

    # The device acknowledges; only its own delivery, only once.
    await alice_ws.send_json({"type": "reminder_ack", "delivery_id": frame["delivery_id"]})
    await settle()
    [delivery] = await h.rows(ReminderDelivery)
    assert delivery.status == "acked"
    await alice_ws.disconnect()
    await bob_ws.disconnect()


async def test_every_active_device_of_the_owner_gets_one(make_harness):
    h = await make_harness(config=ANDROID)
    alice = await h.user("alice")
    # A second phone for the same user, and a revoked third one.
    second = await h.user("alice")
    third = await h.user("alice")
    assert second.user_id == third.user_id == alice.user_id
    async with h.storage.session() as s:
        await s.execute(update(Device).where(Device.device_id == third.device_id).values(revoked=True))
        await s.commit()
    job = await create(h, alice)
    await fire_at(h, job["job_id"])
    assert {d.device_id for d in await h.rows(ReminderDelivery)} == {alice.device_id, second.device_id}


async def test_offline_devices_get_a_content_free_wake_and_the_reminder_on_reconnect(make_harness):
    h = await make_harness(config=ANDROID)
    alice = await h.user("alice")
    wakes = WakeLog()
    firer(h).wake_sender = PayloadWake(wakes)
    job = await create(h, alice, "Call the pharmacy about the prescription")

    fired = await fire_at(h, job["job_id"])
    [delivery] = await h.rows(ReminderDelivery)
    assert delivery.status == "pending"
    [firing] = await h.rows(ScheduledJobFiring)
    assert firing.outcome == "queued"
    assert await h.rows(AuditEvent, AuditEvent.action == AuditAction.SCHEDULER_REMINDER_QUEUED.value)
    # SCH-T4 / ANDC-T9: the push payload carries nothing but "reconnect".
    assert wakes.sent == [(alice.device_id, '{"type":"wake"}')]
    assert "pharmacy" not in json.dumps([p for _, p in wakes.sent])

    # Reconnecting an hour later: it arrives, marked late.
    firer(h).clock = lambda: fired + timedelta(hours=1)
    ws = await phone(h, alice)
    frame = await next_frame(ws)
    assert frame is not None and frame["task_reason"] == "Call the pharmacy about the prescription"
    assert frame["late"] is True
    await settle()
    [firing] = await h.rows(ScheduledJobFiring)
    assert firing.outcome == "delivered"
    await ws.disconnect()


async def test_unacknowledged_reminders_are_resent_until_acked(make_harness):
    h = await make_harness(config=ANDROID)
    alice = await h.user("alice")
    ws = await phone(h, alice)
    job = await create(h, alice)
    await fire_at(h, job["job_id"])
    first = await next_frame(ws)
    await ws.disconnect()

    ws = await phone(h, alice)  # never acked → sent again, same delivery id
    again = await next_frame(ws)
    assert again is not None and again["delivery_id"] == first["delivery_id"]
    await ws.send_json({"type": "reminder_ack", "delivery_id": again["delivery_id"]})
    await settle()
    await ws.disconnect()

    ws = await phone(h, alice)
    assert await next_frame(ws, timeout=0.3) is None
    await ws.disconnect()


async def test_a_client_without_the_reminders_feature_is_never_sent_one(make_harness):
    """An older client would close the socket on an unknown frame; it gets
    none, and the reminder waits for a client that understands it."""

    h = await make_harness(config=ANDROID)
    alice = await h.user("alice")
    ws = await phone(h, alice, features=())
    job = await create(h, alice)
    await fire_at(h, job["job_id"])
    assert await next_frame(ws, timeout=0.3) is None
    [delivery] = await h.rows(ReminderDelivery)
    assert delivery.status == "pending"
    await ws.disconnect()


async def test_acks_only_count_for_the_sockets_own_device(make_harness):
    h = await make_harness(config=ANDROID)
    alice, mallory = await h.user("alice"), await h.user("mallory")
    job = await create(h, alice)
    await fire_at(h, job["job_id"])
    [delivery] = await h.rows(ReminderDelivery)

    ws = await phone(h, mallory)
    await ws.send_json({"type": "reminder_ack", "delivery_id": str(delivery.delivery_id)})
    await settle()
    [delivery] = await h.rows(ReminderDelivery)
    assert delivery.status == "pending"

    await ws.send_json({"type": "reminder_ack", "delivery_id": "not-a-uuid"})
    with pytest.raises(ChannelClosed) as closed:
        while True:
            await ws.receive_text(timeout=2)
    assert closed.value.code == 4008


async def test_without_a_device_channel_reminders_wait_then_expire(make_harness):
    """Device unavailable (Android disabled): nothing is lost silently — the
    reminder is queued, then expires and is audited."""

    h = await make_harness(config={"scheduler": {"pending_delivery_ttl_hours": 1}})
    alice = await h.user("alice")
    job = await create(h, alice)
    fired = await fire_at(h, job["job_id"])
    [delivery] = await h.rows(ReminderDelivery)
    assert delivery.status == "pending"

    await firer(h).tick(now=fired + timedelta(hours=2))
    [delivery] = await h.rows(ReminderDelivery)
    assert delivery.status == "expired"
    assert await h.rows(AuditEvent, AuditEvent.action == AuditAction.SCHEDULER_REMINDER_EXPIRED.value)
    [firing] = await h.rows(ScheduledJobFiring)
    assert firing.outcome == "undeliverable"


async def test_an_owner_with_no_active_device_is_undeliverable(make_harness):
    h = await make_harness(config=ANDROID)
    alice = await h.user("alice")
    job = await create(h, alice)
    async with h.storage.session() as s:
        await s.execute(update(Device).values(revoked=True))
        await s.commit()
    await fire_at(h, job["job_id"])
    assert await h.rows(ReminderDelivery) == []
    [firing] = await h.rows(ScheduledJobFiring)
    assert firing.outcome == "undeliverable"
    assert await h.rows(AuditEvent, AuditEvent.action == AuditAction.SCHEDULER_REMINDER_UNDELIVERABLE.value)


# ── fire-time re-checks (SCH-T3) ──────────────────────────────────────────


async def test_sch_t3_a_suspended_owner_gets_nothing(make_harness):
    h = await make_harness(config=ANDROID)
    alice = await h.user("alice")
    ws = await phone(h, alice)
    job = await create(h, alice)
    async with h.storage.session() as s:
        await s.execute(update(User).where(User.user_id == alice.user_id).values(status=UserStatus.SUSPENDED))
        await s.commit()

    await fire_at(h, job["job_id"])
    assert await next_frame(ws, timeout=0.3) is None
    assert await h.rows(ReminderDelivery) == []
    [firing] = await h.rows(ScheduledJobFiring)
    assert (firing.outcome, firing.reason) == ("cancelled_recheck", "user_not_active")
    [row] = await h.rows(ScheduledJob)
    assert row.status is JobStatus.CANCELLED and row.next_fire_at is None
    [event] = await h.rows(AuditEvent, AuditEvent.action == AuditAction.SCHEDULER_JOB_RECHECK_FAILED.value)
    assert event.resource.endswith(":user_not_active")


async def test_sch_t3_revoked_membership_means_nothing_is_delivered(make_harness):
    h = await make_harness(config=ANDROID)
    alice, bob = await h.user("alice"), await h.user("bob")
    graph = await h.shared_graph(alice, bob)
    job = await create(h, bob, "graph chore", graph_id=str(graph))
    ws = await phone(h, bob)
    r = await h.client.delete(f"{API_V1_PREFIX}/graphs/{graph}/members/{bob.user_id}", headers=bob.auth)
    assert r.status_code == 204, r.text

    await fire_at(h, job["job_id"])
    assert await next_frame(ws, timeout=0.3) is None
    assert await h.rows(ReminderDelivery) == []
    [firing] = await h.rows(ScheduledJobFiring)
    assert (firing.outcome, firing.reason) == ("cancelled_recheck", "membership_revoked")


async def test_queued_reminders_are_rechecked_when_sent(make_harness):
    """A queued reminder can wait hours; cancelling the job, revoking the device
    or suspending the owner meanwhile withdraws it."""

    h = await make_harness(config=ANDROID)
    alice = await h.user("alice")
    second = await h.user("alice")
    job = await create(h, alice)
    await fire_at(h, job["job_id"])
    assert len(await h.rows(ReminderDelivery)) == 2

    async with h.storage.session() as s:
        await s.execute(update(Device).where(Device.device_id == second.device_id).values(revoked=True))
        await s.commit()
    assert await firer(h).device_connected(user_id=alice.user_id, device_id=second.device_id) == 0
    [dropped] = await h.rows(ReminderDelivery, ReminderDelivery.device_id == second.device_id)
    assert dropped.status == "dropped"

    # Cancelling the (already fired) job withdraws what is still queued.
    first = await h.client.delete(f"{JOBS}/{job['job_id']}", headers=alice.auth)
    token = first.json()["error"]["details"]["confirmation_token"]
    ok = await h.client.delete(f"{JOBS}/{job['job_id']}", headers={**alice.auth, "X-Confirmation-Token": token})
    assert ok.status_code == 204
    ws = await phone(h, alice)
    assert await next_frame(ws, timeout=0.3) is None
    assert {d.status for d in await h.rows(ReminderDelivery)} == {"dropped"}
    await ws.disconnect()


# ── idempotency ───────────────────────────────────────────────────────────


async def test_duplicate_scheduler_events_fire_once(make_harness):
    h = await make_harness(config=ANDROID)
    alice = await h.user("alice")
    ws = await phone(h, alice)
    job = await create(h, alice)
    due = await due_of(h, job["job_id"])
    now = due + timedelta(seconds=1)
    firer(h).clock = lambda: now

    results = await asyncio.gather(*(firer(h).fire(uuid.UUID(job["job_id"]), now=now) for _ in range(3)))
    assert results.count(True) == 1
    # A replayed event for the same occurrence, even with the job put back.
    async with h.storage.session() as s:
        await s.execute(update(ScheduledJob).values(status=JobStatus.ACTIVE, next_fire_at=due))
        await s.commit()
    assert await firer(h).fire(uuid.UUID(job["job_id"]), now=now) is False
    assert len(await h.rows(ScheduledJobFiring)) == 1
    assert len(await h.rows(ReminderDelivery)) == 1
    assert (await next_frame(ws)) is not None
    assert await next_frame(ws, timeout=0.3) is None
    await ws.disconnect()


async def test_recurring_jobs_advance_and_stay_active(make_harness):
    h = await make_harness(config=ANDROID)
    alice = await h.user("alice")
    resp = await h.client.post(JOBS, json={"task_reason": "stand up and stretch", "schedule": "0 */2 * * *"},
                               headers=alice.auth)
    job = resp.json()
    first_due = await due_of(h, job["job_id"])
    await fire_at(h, job["job_id"])
    [row] = await h.rows(ScheduledJob)
    assert row.status is JobStatus.ACTIVE
    assert row.next_fire_at.replace(tzinfo=timezone.utc) == first_due + timedelta(hours=2)


# ── SCH-T2: a firing never executes ───────────────────────────────────────


async def test_sch_t2_firing_never_reaches_runtime_tools_devices_or_authority(make_harness, monkeypatch):
    h = await make_harness(config=ANDROID)
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope={"package_name": "com.example"})
    ws = await phone(h, alice)
    # A reminder whose words read like an instruction stays a message.
    job = await create(h, alice, "tap the Pay button in com.example and confirm")

    touched: list[str] = []

    def trap(name):
        async def _trap(*args, **kwargs):
            touched.append(name)
            raise AssertionError(f"{name} reached from a firing")
        return _trap

    runtime = h.app.state.agent_tasks.runtime
    monkeypatch.setattr(runtime, "submit", trap("runtime.submit"))
    monkeypatch.setattr(runtime, "confirm", trap("runtime.confirm"))
    monkeypatch.setattr(runtime._tools, "run", trap("tools.run"))
    monkeypatch.setattr(h.app.state.device_hub, "send", trap("device_hub.send"))
    monkeypatch.setattr(h.core.engine, "authorize", trap("engine.authorize"))
    monkeypatch.setattr(h.core.confirmations, "issue", trap("confirmations.issue"))

    before = {model: len(await h.rows(model)) for model in (AgentTask, UsageEvent, ConfirmationToken,
                                                            CapabilityGrant)}
    await fire_at(h, job["job_id"])
    frame = await next_frame(ws)
    assert frame is not None and frame["type"] == "reminder"
    DeviceReminder.model_validate(frame)  # strict: exactly a reminder, nothing else
    assert touched == []
    assert h.ui.calls == []
    after = {model: len(await h.rows(model)) for model in before}
    assert after == before  # no task, no metered call, no confirmation, no grant
    await ws.disconnect()


async def test_the_reminder_channel_accepts_only_a_reminder(make_harness):
    from server.execution.android import build_operation  # noqa: F401 — the thing that must not pass

    h = await make_harness(config=ANDROID)
    hub = h.app.state.device_hub
    with pytest.raises(TypeError):
        await hub.send_reminder({"type": "operation"}, user_id=uuid.uuid4())  # type: ignore[arg-type]


async def test_offline_wake_goes_through_the_configured_push_waker(make_harness):
    """With push configured (docs/23 §4, `android.push`), the hub's waker is
    asked to wake exactly the owner's offline device — given the device and the
    owner, nothing else. Without one, nothing is sent anywhere."""

    h = await make_harness(config=ANDROID)
    alice = await h.user("alice")

    class RecordingWaker:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        async def wake(self, device_id, *, user_id):
            self.calls.append((device_id, user_id))
            return True

    waker = RecordingWaker()
    h.app.state.device_hub.attach_waker(waker)
    job = await create(h, alice)
    await fire_at(h, job["job_id"])
    assert waker.calls == [(alice.device_id, alice.user_id)]

    # A connected device is not woken.
    ws = await phone(h, alice)
    await next_frame(ws)
    job = await create(h, alice, "second")
    await fire_at(h, job["job_id"])
    assert len(waker.calls) == 1
    await ws.disconnect()
