"""docs/29 Phases 3+4 — cross-user and cross-graph isolation, end to end
(slice 4C; AGENT-T9 extended to everything Phase 3 and 4 added).

Two users who share a graph are still two owners. An agent is its owner's
alone (docs/29 §6: owner-private), so sharing a graph gives the other member
nothing of it — not its runs, its reminder, its tap, the Judge's suggestions
for it, or its notebook — and every refusal is `404`, indistinguishable from
an agent that does not exist. Leaving the agent's graph ends what the agent
could do there: its reminder is no longer delivered, and nothing — a tap
included — runs it outside that graph.
"""

from __future__ import annotations

import uuid
from datetime import timezone

import pytest

from server.security.superuser import SUPERUSER_TOKEN_ENV
from server.storage.models import (
    AgentDefinitionRow,
    AgentRunRow,
    AgentTask,
    ImprovementCandidateRow,
    ReminderDelivery,
    ScheduledJob,
)
from shared.schemas.enums import JobStatus
from tests.agents.harness import AGENTS_ON, API, DRAFT, create_agent
from tests.evaluation.conftest import TOKEN, drain, judge_config, verdict
from tests.runtime.conftest import ScriptedModel, final
from tests.scheduler.test_firing import next_frame, phone

REMINDER_DRAFT = {
    **DRAFT,
    "trigger_request": {"kind": "reminder", "cron": "0 7 * * *", "timezone": "Asia/Kolkata"},
}
BETTER = "Summarize only the critical advisories, newest first."


@pytest.fixture
async def h(make_harness, monkeypatch):
    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, TOKEN)
    judge = ScriptedModel("scripted-judge")
    harness = await make_harness(config={**AGENTS_ON, **judge_config(), "android": {"enabled": True}},
                                 agent_tools=True, models={"scripted-judge": judge})
    harness.judge = judge
    return harness


async def _fire(h, job: ScheduledJob) -> bool:
    return await h.app.state.reminders.firer.fire(job.job_id, now=job.next_fire_at.replace(tzinfo=timezone.utc))


async def test_a_member_of_the_same_graph_reaches_nothing_of_anothers_agent(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    graph = await h.shared_graph(alice, bob)
    agent = await create_agent(h, alice, draft=REMINDER_DRAFT)
    [definition] = await h.rows(AgentDefinitionRow)
    assert definition.graph_id == graph              # made in the shared graph, still alice's alone
    agent_id = agent["agent_id"]

    # A judged run of alice's, so there is a run, a suggestion and a notebook to reach for.
    h.judge.push(verdict(quality=0.6, improvement_candidates=[
        {"target": "agent.purpose", "proposed_change": BETTER, "expected_effect": "shorter"}]))
    h.model.push(final("Two critical advisories."))
    run = await h.client.post(f"{API}/{agent_id}/runs", json={}, headers=alice.auth)
    assert run.status_code == 202, run.text
    await drain(h)
    [candidate] = await h.rows(ImprovementCandidateRow)

    # Alice's phone gets the reminder; bob's sees nothing of it.
    alices_phone = await phone(h, alice, features=("reminders", "agent_reminders"))
    bobs_phone = await phone(h, bob, features=("reminders", "agent_reminders"))
    [job] = await h.rows(ScheduledJob)
    assert await _fire(h, job)
    frame = await next_frame(alices_phone)
    assert frame is not None and frame["agent_id"] == agent_id
    assert await next_frame(bobs_phone, timeout=0.3) is None
    await alices_phone.disconnect()
    await bobs_phone.disconnect()

    run_id = run.json()["run_id"]
    for method, path, body in [
        ("GET", f"{API}/{agent_id}", None),
        ("POST", f"{API}/{agent_id}/runs", {}),
        ("POST", f"{API}/{agent_id}/runs", {"reminder_delivery_id": frame["delivery_id"]}),
        ("GET", f"{API}/{agent_id}/runs", None),
        ("GET", f"{API}/{agent_id}/runs/{run_id}", None),
        ("POST", f"{API}/{agent_id}/runs/{run_id}/cancel", {}),
        ("POST", f"{API}/{agent_id}/pause", {}),
        ("POST", f"{API}/{agent_id}/resume", {}),
        ("GET", f"{API}/{agent_id}/notebook", None),
        ("GET", f"{API}/{agent_id}/export", None),
        ("GET", f"{API}/{agent_id}/candidates", None),
        ("POST", f"{API}/{agent_id}/candidates/{candidate.candidate_id}/compile", {}),
        ("POST", f"{API}/{agent_id}/candidates/{candidate.candidate_id}/dismiss", {}),
        ("DELETE", f"/api/v1/jobs/{job.job_id}", None),
    ]:
        resp = await h.client.request(method, path, json=body, headers=bob.auth)
        assert resp.status_code == 404, (method, path, resp.status_code, resp.text)
    # Nor do they show up in bob's own lists.
    assert (await h.client.get(API, headers=bob.auth)).json()["items"] == []
    assert (await h.client.get("/api/v1/jobs", headers=bob.auth)).json()["items"] == []
    # And none of it changed anything of alice's.
    [definition] = await h.rows(AgentDefinitionRow)
    assert definition.status == "active"
    assert len(await h.rows(AgentRunRow)) == 1
    [candidate] = await h.rows(ImprovementCandidateRow)
    assert candidate.status == "pending"
    [job] = await h.rows(ScheduledJob)
    assert job.status is JobStatus.ACTIVE


async def test_leaving_the_agents_graph_ends_its_reminder_and_its_runs_there(h):
    owner, alice = await h.user("owner"), await h.user("alice")
    graph = await h.shared_graph(owner, alice)
    agent = await create_agent(h, alice, draft=REMINDER_DRAFT)
    [job] = await h.rows(ScheduledJob)

    # A reminder delivered while alice is a member…
    ws = await phone(h, alice, features=("reminders", "agent_reminders"))
    assert await _fire(h, job)
    frame = await next_frame(ws)
    assert frame is not None and frame["agent_id"] == agent["agent_id"]
    await ws.send_json({"type": "reminder_ack", "delivery_id": frame["delivery_id"]})

    # …then she is removed from the graph.
    removed = await h.client.delete(f"/api/v1/graphs/{graph}/members/{alice.user_id}", headers=owner.auth)
    assert removed.status_code == 204, removed.text
    # Her earlier reminder's tap runs nothing outside that graph.
    tap = await h.client.post(f"{API}/{agent['agent_id']}/runs",
                              json={"reminder_delivery_id": frame["delivery_id"]}, headers=alice.auth)
    assert tap.status_code in (404, 409), tap.text
    assert await h.rows(AgentRunRow) == [] and await h.rows(AgentTask) == []
    # And the next firing re-checks her and delivers nothing.
    [job] = await h.rows(ScheduledJob)
    delivered_before = len(await h.rows(ReminderDelivery))
    await _fire(h, job)
    assert await next_frame(ws, timeout=0.3) is None
    assert len(await h.rows(ReminderDelivery)) == delivered_before
    await ws.disconnect()


async def test_an_agent_is_never_runnable_from_a_graph_it_does_not_belong_to(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    agent = await create_agent(h, alice)                 # alice's personal graph
    graph = await h.shared_graph(alice, bob)             # alice's session now in the shared graph
    assert graph is not None
    resp = await h.client.post(f"{API}/{agent['agent_id']}/runs", json={}, headers=alice.auth)
    assert resp.status_code == 409 and resp.json()["error"]["details"]["reason"] == "graph_mismatch", resp.text
    assert await h.rows(AgentRunRow) == []


async def test_one_users_agent_id_in_anothers_reminder_payload_is_just_data(h):
    """An agent id travels only in the owner's own frames; a request can never
    put one on a job (POST /jobs rejects it), and a stranger's id in a tap is
    `404` — the server derives who is asking from the session, never from it."""

    alice, bob = await h.user("alice"), await h.user("bob")
    agent = await create_agent(h, alice, draft=REMINDER_DRAFT)
    forged = await h.client.post("/api/v1/jobs", json={"task_reason": "x", "schedule": "CRON_TZ=UTC 0 9 * * *",
                                                       "agent_id": agent["agent_id"]}, headers=bob.auth)
    assert forged.status_code == 422, forged.text
    tap = await h.client.post(f"{API}/{agent['agent_id']}/runs",
                              json={"reminder_delivery_id": str(uuid.uuid4())}, headers=bob.auth)
    assert tap.status_code == 404, tap.text
    assert await h.rows(AgentRunRow) == []
