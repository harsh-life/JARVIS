"""docs/29 §17 — an agent's reminder (Phase 4, slice 4A; AGENT-T10 reminder
half, AGENT-T35).

An agent compiled with a `reminder` trigger gets an ordinary scheduler job,
created through the scheduler's own service and the one engine — never by the
scheduler importing the factory. The job carries the agent's id as *data*
(`scheduled_jobs.agent_id`); the scheduler never interprets it. What must hold:

* the reminder is the owner's own, private, worded "Run agent: {name}" (the
  name the owner approved on the card), on exactly the compiled schedule;
* a firing reminder delivers a message and nothing else — no task, no run, no
  token, no tool call (docs/22 §0, docs/29 §17.2);
* only a client that declared `agent_reminders` receives the `agent_id`; an
  older one gets the plain reminder, never a frame it would reject;
* pausing (owner or operator), deleting, or re-approval suppresses the
  reminder; resuming brings it back; an update replaces it;
* with agents off — or the scheduler off — nothing of this exists.
"""

from __future__ import annotations

import uuid
from datetime import timezone

import pytest

from server.security.superuser import SUPERUSER_TOKEN_ENV
from server.storage.models import (
    AgentDefinitionRow,
    AgentRunRow,
    AgentRunTokenRow,
    AgentTask,
    ReminderDelivery,
    ScheduledJob,
)
from shared.schemas.device_channel import DeviceReminder
from shared.schemas.enums import JobStatus, Visibility
from tests.agents.harness import AGENTS_ON, API, DRAFT, HEADER, call_json, create_agent, last_compile_id, run_agent
from tests.agents.test_agent_runs import _swap_registries
from tests.evaluation.conftest import SU, TOKEN
from tests.runtime.conftest import ask, final, pending_of
from tests.scheduler.test_firing import REMINDER_KEYS, next_frame, phone, settle

REMINDER_DRAFT = {
    **DRAFT,
    "trigger_request": {"kind": "reminder", "cron": "0 7 * * *", "timezone": "Asia/Kolkata"},
}
ON = {**AGENTS_ON, "android": {"enabled": True}}
ADMIN = "/api/v1/admin"


@pytest.fixture
async def h(make_harness):
    return await make_harness(config=ON, agent_tools=True)


async def _jobs(h, *, active: bool | None = True) -> list[ScheduledJob]:
    where = [ScheduledJob.agent_id.is_not(None)]
    if active is not None:
        where.append((ScheduledJob.status == JobStatus.ACTIVE) if active else (ScheduledJob.status != JobStatus.ACTIVE))
    return await h.rows(ScheduledJob, *where)


async def _confirmed(h, actor, method: str, path: str, body: dict | None = None):
    first = await h.client.request(method, path, json=body if body is not None else {}, headers=actor.auth)
    assert first.status_code == 403, first.text
    token = first.json()["error"]["details"]["confirmation_token"]
    return await h.client.request(method, path, json=body if body is not None else {},
                                  headers={**actor.auth, HEADER: token})


async def _fire(h, job: ScheduledJob) -> None:
    firer = h.app.state.reminders.firer
    assert await firer.fire(job.job_id, now=job.next_fire_at.replace(tzinfo=timezone.utc))


# ── the job ──────────────────────────────────────────────────────────────


async def test_a_reminder_agent_gets_one_ordinary_private_job(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=REMINDER_DRAFT)
    [job] = await _jobs(h)
    assert str(job.agent_id) == agent["agent_id"]
    assert (job.owner_user_id, job.source_user_id) == (alice.user_id, alice.user_id)
    assert job.visibility is Visibility.PRIVATE
    assert job.task_reason == f"Run agent: {DRAFT['name']}"
    assert job.schedule == "CRON_TZ=Asia/Kolkata 0 7 * * *"
    assert job.next_fire_at is not None
    # It is the owner's reminder like any other: listed, and cancellable by them.
    listed = (await h.client.get("/api/v1/jobs", headers=alice.auth)).json()["items"]
    assert [(j["job_id"], j["agent_id"]) for j in listed] == [(str(job.job_id), agent["agent_id"])]


async def test_an_on_demand_agent_has_no_job(h):
    alice = await h.user("alice")
    await create_agent(h, alice)
    assert await h.rows(ScheduledJob) == []


async def test_another_user_sees_nothing_of_it(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    await create_agent(h, alice, draft=REMINDER_DRAFT)
    assert (await h.client.get("/api/v1/jobs", headers=bob.auth)).json()["items"] == []


# ── firing delivers a message, never executes ────────────────────────────


async def test_a_firing_reminder_runs_nothing(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=REMINDER_DRAFT)
    ws = await phone(h, alice, features=("reminders", "agent_reminders"))
    [job] = await _jobs(h)
    await _fire(h, job)
    frame = await next_frame(ws)
    assert frame is not None and frame["type"] == "reminder", frame
    assert set(frame) == REMINDER_KEYS | {"agent_id"}
    assert frame["agent_id"] == agent["agent_id"]
    assert frame["task_reason"] == f"Run agent: {DRAFT['name']}"
    DeviceReminder.model_validate(frame)
    await settle()
    # A message, nothing else: no task, no run, no token.
    assert await h.rows(AgentTask) == [] and await h.rows(AgentRunRow) == []
    assert await h.rows(AgentRunTokenRow) == []
    await ws.disconnect()


async def test_an_older_client_gets_the_plain_reminder(h):
    alice = await h.user("alice")
    await create_agent(h, alice, draft=REMINDER_DRAFT)
    ws = await phone(h, alice, features=("reminders",))
    [job] = await _jobs(h)
    await _fire(h, job)
    frame = await next_frame(ws)
    assert frame is not None and set(frame) == REMINDER_KEYS, frame
    await ws.disconnect()


async def test_a_plain_reminder_never_carries_an_agent(h):
    alice = await h.user("alice")
    ws = await phone(h, alice, features=("reminders", "agent_reminders"))
    resp = await h.client.post("/api/v1/jobs", json={"task_reason": "Water the plants",
                                                     "schedule": "CRON_TZ=UTC 0 9 * * *"}, headers=alice.auth)
    assert resp.status_code == 201, resp.text
    [job] = await h.rows(ScheduledJob)
    assert job.agent_id is None
    await _fire(h, job)
    frame = await next_frame(ws)
    assert frame is not None and set(frame) == REMINDER_KEYS, frame
    await ws.disconnect()


async def test_a_job_request_cannot_name_an_agent(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    resp = await h.client.post("/api/v1/jobs", json={"task_reason": "x", "schedule": "CRON_TZ=UTC 0 9 * * *",
                                                     "agent_id": agent["agent_id"]}, headers=alice.auth)
    assert resp.status_code == 422, resp.text
    assert await h.rows(ScheduledJob) == []


# ── the reminder follows the agent's lifecycle ───────────────────────────


async def test_pausing_suppresses_and_resuming_restores_the_reminder(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=REMINDER_DRAFT)
    resp = await h.client.post(f"{API}/{agent['agent_id']}/pause", json={}, headers=alice.auth)
    assert resp.status_code == 200, resp.text
    assert await _jobs(h) == [] and len(await _jobs(h, active=False)) == 1
    resumed = await _confirmed(h, alice, "POST", f"{API}/{agent['agent_id']}/resume")
    assert resumed.status_code == 200, resumed.text
    [job] = await _jobs(h)
    assert str(job.agent_id) == agent["agent_id"]


async def test_deleting_the_agent_cancels_its_reminder(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=REMINDER_DRAFT)
    ws = await phone(h, alice, features=("reminders", "agent_reminders"))
    deleted = await _confirmed(h, alice, "DELETE", f"{API}/{agent['agent_id']}")
    assert deleted.status_code == 204, deleted.text
    assert await _jobs(h) == []
    [job] = await _jobs(h, active=False)
    assert job.status is JobStatus.CANCELLED
    # A cancelled job never fires.
    assert not await h.app.state.reminders.firer.fire(job.job_id)
    assert await next_frame(ws, timeout=0.3) is None
    await ws.disconnect()


async def test_an_update_replaces_the_reminder(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=REMINDER_DRAFT)
    later = {**REMINDER_DRAFT, "trigger_request": {"kind": "reminder", "cron": "30 8 * * 1-5",
                                                   "timezone": "Asia/Kolkata"}}
    compiled = await h.client.post(f"{API}/compile", params={"agent_id": agent["agent_id"]}, json=later,
                                   headers=alice.auth)
    assert compiled.json()["kind"] == "compiled", compiled.text
    updated = await _confirmed(h, alice, "PATCH", f"{API}/{agent['agent_id']}",
                               {"compile_id": compiled.json()["compile_id"]})
    assert updated.status_code == 200, updated.text
    [job] = await _jobs(h)
    assert job.schedule == "CRON_TZ=Asia/Kolkata 30 8 * * 1-5"
    assert len(await _jobs(h, active=False)) == 1
    # Back to on demand: no reminder at all.
    on_demand = await h.client.post(f"{API}/compile", params={"agent_id": agent["agent_id"]}, json=DRAFT,
                                    headers=alice.auth)
    again = await _confirmed(h, alice, "PATCH", f"{API}/{agent['agent_id']}",
                             {"compile_id": on_demand.json()["compile_id"]})
    assert again.status_code == 200, again.text
    assert await _jobs(h) == []


async def test_a_reminder_agent_without_a_scheduler_is_refused(make_harness):
    h = await make_harness(config={**AGENTS_ON, "scheduler": {"enabled": False}}, agent_tools=True)
    alice = await h.user("alice")
    compiled = await h.client.post(f"{API}/compile", json=REMINDER_DRAFT, headers=alice.auth)
    assert compiled.status_code == 200 and compiled.json()["kind"] == "compiled"
    # Refused before anything is confirmed or written.
    created = await h.client.post(API, json={"compile_id": compiled.json()["compile_id"]}, headers=alice.auth)
    assert created.status_code == 409, created.text
    assert created.json()["error"]["details"]["reason"] == "reminders_unavailable"
    assert (await h.client.get(API, headers=alice.auth)).json()["items"] == []


async def test_with_agents_disabled_no_job_carries_an_agent(make_harness):
    h = await make_harness(config={"android": {"enabled": True}})
    alice = await h.user("alice")
    resp = await h.client.post("/api/v1/jobs", json={"task_reason": "Stretch",
                                                     "schedule": "CRON_TZ=UTC 0 9 * * *"}, headers=alice.auth)
    assert resp.status_code == 201
    [job] = await h.rows(ScheduledJob)
    assert job.agent_id is None and "agent_id" not in resp.json() or resp.json()["agent_id"] is None
    assert await h.rows(ReminderDelivery) == []


async def test_an_operator_pause_cancels_the_reminder_and_release_does_not_restore_it(make_harness, monkeypatch):
    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, TOKEN)
    h = await make_harness(config=ON, agent_tools=True)
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=REMINDER_DRAFT)
    paused = await h.client.post(f"{ADMIN}/control/agents/{agent['agent_id']}/pause", json={"reason": "abuse"},
                                 headers=SU)
    assert paused.status_code == 200, paused.text
    assert await _jobs(h) == []
    released = await h.client.post(f"{ADMIN}/control/agents/{agent['agent_id']}/release",
                                   json={"reason": "reviewed"}, headers=SU)
    assert released.status_code == 200, released.text
    assert await _jobs(h) == []                 # the operator restores nothing
    resumed = await _confirmed(h, alice, "POST", f"{API}/{agent['agent_id']}/resume")
    assert resumed.status_code == 200, resumed.text
    assert len(await _jobs(h)) == 1             # the owner's confirmed resume does


async def test_an_agent_awaiting_re_approval_is_not_reminded(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=REMINDER_DRAFT)
    registries = h.app.state.agent_factory._factory.service.registries
    template = registries.templates["research_digest"]
    _swap_registries(h, enabled_templates={
        **registries.enabled_templates,
        "research_digest": template.model_copy(update={"version": template.version + 1})})
    resp = await run_agent(h, alice, agent["agent_id"])
    assert resp.status_code == 409 and resp.json()["error"]["details"]["reason"] == "needs_reapproval"
    assert await _jobs(h) == []


async def test_the_reminder_quota_refuses_the_whole_agent(make_harness):
    h = await make_harness(config={**ON, "scheduler": {"max_active_jobs_per_user": 1}}, agent_tools=True)
    alice = await h.user("alice")
    plain = await h.client.post("/api/v1/jobs", json={"task_reason": "Stretch", "schedule": "CRON_TZ=UTC 0 9 * * *"},
                                headers=alice.auth)
    assert plain.status_code == 201, plain.text
    compiled = await h.client.post(f"{API}/compile", json=REMINDER_DRAFT, headers=alice.auth)
    created = await _confirmed(h, alice, "POST", API, {"compile_id": compiled.json()["compile_id"]})
    assert created.status_code == 429, created.text
    # Nothing half-made: no agent, no second job.
    assert await h.rows(AgentDefinitionRow) == [] and len(await h.rows(ScheduledJob)) == 1


# ── the worker's path (agent.define in a chat task) ──────────────────────


async def test_a_reminder_agent_made_in_a_task_gets_its_job_and_loses_it_on_delete(h):
    alice = await h.user("alice")
    await h.grant(alice, "agent.define")
    h.model.push(ask("agent.define"), call_json("agent.define", "compile", REMINDER_DRAFT),
                 lambda m: call_json("agent.define", "create", {"compile_id": last_compile_id(m)}))
    paused = pending_of(await h.submit(alice, "remind me each morning to run my digest"))
    h.model.push(final("Created."))
    assert (await h.confirm(alice, paused["task_id"], paused["pending"]["confirmation_token"])).status_code == 200
    [definition] = await h.rows(AgentDefinitionRow)
    [job] = await _jobs(h)
    assert (job.agent_id, job.owner_user_id) == (definition.agent_id, alice.user_id)
    assert job.task_reason == f"Run agent: {DRAFT['name']}" and job.visibility is Visibility.PRIVATE

    await h.grant(alice, "agent.delete")
    h.model.push(ask("agent.delete"), call_json("agent.delete", "delete", {}, ref=str(definition.agent_id)))
    paused = pending_of(await h.submit(alice, "delete my digest agent"))
    h.model.push(final("Deleted."))
    assert (await h.confirm(alice, paused["task_id"], paused["pending"]["confirmation_token"])).status_code == 200
    assert await _jobs(h) == []


# ── the frame itself ─────────────────────────────────────────────────────


def test_a_plain_reminder_frame_is_byte_identical_for_every_client():
    """SCH-B11's pattern: a client that never heard of agents gets exactly the
    frame it always got — no `agent_id`, not even a null."""

    plain = DeviceReminder(delivery_id=uuid.uuid4(), job_id=uuid.uuid4(), device_id=uuid.uuid4(),
                           task_reason="Water the plants", scheduled_for="2026-10-01T03:30:00Z")
    legacy = plain.model_dump_json(exclude={"agent_id"})
    assert plain.wire(agent_reminders=False) == legacy
    assert plain.wire(agent_reminders=True) == legacy
    with_agent = plain.model_copy(update={"agent_id": uuid.uuid4()})
    assert with_agent.wire(agent_reminders=False) == legacy
    assert '"agent_id"' in with_agent.wire(agent_reminders=True)


async def test_an_agents_reminder_is_decided_by_the_one_engine_like_any_reminder(h, monkeypatch):
    """The reminder is not implied by the agent: the engine decides the
    scheduled job on its own (D1 on its graph). Refused there, nothing is
    created — not the agent, not the job."""

    import dataclasses

    from shared.schemas.authorization import ResourceType

    reminders = h.app.state.agent_factory._factory.reminders
    engine = reminders._core.engine
    decide = engine.authorize

    async def outside_the_graph(session, request, **kwargs):
        if request.resource_type is ResourceType.SCHEDULEDJOB:
            request = dataclasses.replace(request, graph_id=uuid.uuid4())
        return await decide(session, request, **kwargs)

    monkeypatch.setattr(engine, "authorize", outside_the_graph)
    alice = await h.user("alice")
    compiled = await h.client.post(f"{API}/compile", json=REMINDER_DRAFT, headers=alice.auth)
    created = await _confirmed(h, alice, "POST", API, {"compile_id": compiled.json()["compile_id"]})
    assert created.status_code in (403, 404), created.text
    assert await h.rows(AgentDefinitionRow) == [] and await h.rows(ScheduledJob) == []
