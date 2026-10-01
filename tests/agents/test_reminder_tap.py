"""docs/29 §17.1 — the reminder tap (Phase 4, slice 4B; AGENT-T10).

A firing reminder delivers "Run agent: {name}" to the owner's phone and runs
nothing. Tapping "Run agent" on it is the owner, present, asking for an
ordinary run through the authenticated session — `POST /agents/{id}/runs`,
decided by the server exactly as an on-demand run. The tap names the delivery
it came from (`reminder_delivery_id`) only so the run can say so
(`kind = reminder_tap`) and so a double tap never starts two runs. That id is
checked against the authenticated caller, never trusted: it must be a
reminder this very device received, for this very agent, of this very owner.
It grants nothing: every check an on-demand run passes, a tapped run passes.
"""

from __future__ import annotations

import uuid
from datetime import timezone

import pytest

from server.storage.models import AgentRunRow, AgentTask, ReminderDelivery, ScheduledJob
from tests.agents.harness import AGENTS_ON, API, DRAFT, create_agent, run_agent
from tests.runtime.conftest import final
from tests.scheduler.test_firing import next_frame, phone

REMINDER_DRAFT = {
    **DRAFT,
    "trigger_request": {"kind": "reminder", "cron": "0 7 * * *", "timezone": "Asia/Kolkata"},
}
ON = {**AGENTS_ON, "android": {"enabled": True}}


@pytest.fixture
async def h(make_harness):
    return await make_harness(config=ON, agent_tools=True)


async def _delivered(h, actor, agent: dict) -> dict:
    """Fire the agent's reminder to `actor`'s phone; the frame it received."""

    ws = await phone(h, actor, features=("reminders", "agent_reminders"))
    [job] = await h.rows(ScheduledJob, ScheduledJob.agent_id == uuid.UUID(agent["agent_id"]))
    assert await h.app.state.reminders.firer.fire(job.job_id, now=job.next_fire_at.replace(tzinfo=timezone.utc))
    frame = await next_frame(ws)
    assert frame is not None and frame["agent_id"] == agent["agent_id"], frame
    await ws.send_json({"type": "reminder_ack", "delivery_id": frame["delivery_id"]})
    await ws.disconnect()
    return frame


async def _tap(h, actor, agent_id: str, delivery_id: str):
    return await h.client.post(f"{API}/{agent_id}/runs", json={"reminder_delivery_id": delivery_id},
                               headers=actor.auth)


async def test_a_tap_is_an_ordinary_run_of_the_present_owner(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=REMINDER_DRAFT)
    frame = await _delivered(h, alice, agent)
    # Firing ran nothing.
    assert await h.rows(AgentRunRow) == [] and await h.rows(AgentTask) == []
    h.model.push(final("Two critical advisories."))
    resp = await _tap(h, alice, agent["agent_id"], frame["delivery_id"])
    assert resp.status_code == 202, resp.text
    view = resp.json()
    assert (view["kind"], view["status"]) == ("reminder_tap", "completed")
    [run] = await h.rows(AgentRunRow)
    assert (run.kind, str(run.reminder_delivery_id)) == ("reminder_tap", frame["delivery_id"])
    # The run is alice's own task, from this session's device — like any run.
    [task] = await h.rows(AgentTask)
    assert (task.user_id, task.device_id) == (alice.user_id, alice.device_id)


async def test_the_same_tap_twice_starts_one_run(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=REMINDER_DRAFT)
    frame = await _delivered(h, alice, agent)
    h.model.push(final("done"))
    first = await _tap(h, alice, agent["agent_id"], frame["delivery_id"])
    again = await _tap(h, alice, agent["agent_id"], frame["delivery_id"])
    assert first.status_code == 202 and again.status_code == 202, again.text
    assert again.json()["run_id"] == first.json()["run_id"]
    assert len(await h.rows(AgentRunRow)) == 1


async def test_an_untapped_run_stays_on_demand(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=REMINDER_DRAFT)
    h.model.push(final("done"))
    resp = await run_agent(h, alice, agent["agent_id"])
    assert resp.status_code == 202 and resp.json()["kind"] == "on_demand", resp.text


async def test_a_tap_cannot_be_borrowed_by_another_user(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    agent = await create_agent(h, alice, draft=REMINDER_DRAFT)
    bobs = await create_agent(h, bob, draft=REMINDER_DRAFT)
    frame = await _delivered(h, alice, agent)
    # Bob can neither run alice's agent with it nor label his own run with it.
    assert (await _tap(h, bob, agent["agent_id"], frame["delivery_id"])).status_code == 404
    assert (await _tap(h, bob, bobs["agent_id"], frame["delivery_id"])).status_code == 404
    assert await h.rows(AgentRunRow) == []


async def test_a_tap_names_exactly_its_own_agent(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=REMINDER_DRAFT)
    other = await create_agent(h, alice, draft={**REMINDER_DRAFT, "name": "Second digest"})
    frame = await _delivered(h, alice, agent)
    assert (await _tap(h, alice, other["agent_id"], frame["delivery_id"])).status_code == 404
    assert await h.rows(AgentRunRow) == []


async def test_a_tap_comes_from_the_device_that_received_it(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=REMINDER_DRAFT)
    frame = await _delivered(h, alice, agent)
    other_phone = await h.user("alice")          # alice's second device
    assert other_phone.user_id == alice.user_id and other_phone.device_id != alice.device_id
    assert (await _tap(h, other_phone, agent["agent_id"], frame["delivery_id"])).status_code == 404
    assert await h.rows(AgentRunRow) == []


async def test_an_undelivered_or_unknown_reminder_is_not_a_tap(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=REMINDER_DRAFT)
    [job] = await h.rows(ScheduledJob)
    # Fired while the phone is offline: the delivery waits, unsent.
    assert await h.app.state.reminders.firer.fire(job.job_id, now=job.next_fire_at.replace(tzinfo=timezone.utc))
    [pending] = await h.rows(ReminderDelivery)
    assert pending.status == "pending"
    assert (await _tap(h, alice, agent["agent_id"], str(pending.delivery_id))).status_code == 404
    assert (await _tap(h, alice, agent["agent_id"], str(uuid.uuid4()))).status_code == 404
    assert await h.rows(AgentRunRow) == []


async def test_a_tap_passes_every_check_an_on_demand_run_does(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=REMINDER_DRAFT)
    frame = await _delivered(h, alice, agent)
    assert (await h.client.post(f"{API}/{agent['agent_id']}/pause", json={}, headers=alice.auth)).status_code == 200
    resp = await _tap(h, alice, agent["agent_id"], frame["delivery_id"])
    assert resp.status_code == 409, resp.text
    assert await h.rows(AgentRunRow) == []


@pytest.mark.parametrize("body", [{"kind": "unattended"}, {"agent_id": str(uuid.uuid4())},
                                  {"reminder_delivery_id": "not-an-id"}, {"principal": "bob"}])
async def test_a_tap_request_names_nothing_else(h, body):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=REMINDER_DRAFT)
    resp = await h.client.post(f"{API}/{agent['agent_id']}/runs", json=body, headers=alice.auth)
    assert resp.status_code == 422, resp.text
    assert await h.rows(AgentRunRow) == []


async def test_two_taps_racing_still_start_one_run(h, monkeypatch):
    """The second of two simultaneous taps can miss the first one's run when
    it looks; the store's uniqueness then answers, and it gets that run."""

    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=REMINDER_DRAFT)
    frame = await _delivered(h, alice, agent)
    h.model.push(final("done"))
    first = await _tap(h, alice, agent["agent_id"], frame["delivery_id"])
    assert first.status_code == 202, first.text
    service = h.app.state.agent_factory._factory.service
    looked = service.run_for_delivery
    calls: list[int] = []

    async def raced(session, delivery_id):
        calls.append(1)
        return None if len(calls) == 1 else await looked(session, delivery_id)

    monkeypatch.setattr(service, "run_for_delivery", raced)
    again = await _tap(h, alice, agent["agent_id"], frame["delivery_id"])
    assert again.status_code == 202, again.text
    assert again.json()["run_id"] == first.json()["run_id"]
    assert len(await h.rows(AgentRunRow)) == 1 and len(await h.rows(AgentTask)) == 1


def test_the_shared_agent_samples_are_current_and_the_servers_own():
    """Regenerate: `python -m tests.tools.export_agent_samples export`. The
    Android client parses the same file (android/contract AgentSampleTest)."""

    import json

    from shared.schemas.agent_factory import AgentListResponse, AgentRunView, RunAgentRequest
    from shared.schemas.errors import ErrorEnvelope
    from tests.tools.export_agent_samples import PATH, render_file

    assert PATH.read_text(encoding="ascii") == render_file()
    sample = json.loads(PATH.read_text(encoding="ascii"))
    AgentListResponse.model_validate(sample["agent_list"])
    for name in ("run_completed", "run_waiting"):
        assert AgentRunView.model_validate(sample[name]).kind == "reminder_tap"
    ErrorEnvelope.model_validate(sample["run_refused"])
    assert RunAgentRequest.model_validate(sample["run_tap_request"]).reminder_delivery_id is not None


async def test_the_same_tap_answers_with_its_run_even_after_a_pause(h):
    """A repeated tap is the same request: it gets the run it started, never a
    new decision — so pausing in between neither refuses it nor runs again."""

    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=REMINDER_DRAFT)
    frame = await _delivered(h, alice, agent)
    h.model.push(final("done"))
    first = await _tap(h, alice, agent["agent_id"], frame["delivery_id"])
    assert first.status_code == 202, first.text
    assert (await h.client.post(f"{API}/{agent['agent_id']}/pause", json={}, headers=alice.auth)).status_code == 200
    again = await _tap(h, alice, agent["agent_id"], frame["delivery_id"])
    assert again.status_code == 202, again.text
    assert again.json()["run_id"] == first.json()["run_id"] and again.json()["status"] == "completed"
    assert len(await h.rows(AgentRunRow)) == 1
    # A fresh, untapped run of the paused agent is still refused.
    assert (await run_agent(h, alice, agent["agent_id"])).status_code == 409
