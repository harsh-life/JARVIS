"""docs/29 §14 — pausing, resuming and stopping agents and runs (Phase 2 E).

Stop is the safe direction: it needs no confirmation, it is never refused to
the owner, and what it stops does not continue — no hidden tool call after a
stop, no confirmation that can still be redeemed. Resume is the authority-
granting direction: it re-runs the checks a new run would face and is
confirmed by the owner like any change to the agent. Every transition is
deterministic and audited; another user's agent or run is `404`.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from server.storage.models import AgentDefinitionRow, AgentRunRow, AgentTask, AuditEvent
from tests.agents.harness import AGENTS_ON, API, HEADER, create_agent, run_agent
from tests.agents.test_envelope_gate import READ_DRAFT, REPORTS
from tests.runtime.conftest import ask, call, final


@pytest.fixture
async def h(make_harness):
    return await make_harness(config=AGENTS_ON, agent_tools=True)


async def _run(h, actor, agent_id: str) -> dict:
    resp = await run_agent(h, actor, agent_id)
    assert resp.status_code == 202, resp.text
    return resp.json()


async def _cancel(h, actor, agent_id: str, run_id: str):
    return await h.client.post(f"{API}/{agent_id}/runs/{run_id}/cancel", json={}, headers=actor.auth)


async def _paused_run(h, actor, agent_id: str) -> dict:
    """A run paused on the owner's confirmation of an in-envelope activation."""

    h.model.push(ask("file.read", scope=REPORTS), call("files.read", "list_directory", scope=REPORTS),
                 final("done"))
    view = await _run(h, actor, agent_id)
    assert view["status"] == "waiting", view
    return view


async def _audit(h, action: str) -> list[AuditEvent]:
    return await h.rows(AuditEvent, AuditEvent.action == action)


# ── stopping a run ─────────────────────────────────────────────────────────


async def test_cancelling_a_paused_run_drops_its_action_for_good(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    view = await _paused_run(h, alice, agent["agent_id"])
    resp = await _cancel(h, alice, agent["agent_id"], view["run_id"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    [run] = await h.rows(AgentRunRow)
    [task] = await h.rows(AgentTask)
    assert (run.status, task.status) == ("cancelled", "cancelled")
    # The confirmation can no longer be redeemed.
    confirm = await h.confirm(alice, view["task"]["task_id"], view["task"]["pending"]["confirmation_token"])
    assert confirm.status_code in (404, 409)
    assert h.reads.calls == []
    assert len(await _audit(h, "agent.run.cancelled")) == 1


async def test_cancelling_a_running_run_stops_it_before_its_next_tool_call(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    await h.grant(alice, "file.read", resource_scope=REPORTS)
    state: dict = {}

    async def cancel_mid_run(messages):
        # The run is live in this process; its id is its binding's.
        [live] = [t for t in h.runtime._states.live() if t.agent is not None]
        # The runtime races the model call against the cancel signal, so this
        # coroutine is itself cancelled by the stop; the request is shielded.
        state["cancel"] = asyncio.ensure_future(_cancel(h, alice, agent["agent_id"], str(live.agent.run_id)))
        await asyncio.shield(state["cancel"])
        return call("files.read", "list_directory", scope=REPORTS)

    h.model.push(ask("file.read", scope=REPORTS), cancel_mid_run, final("never reached"))
    view = await _run(h, alice, agent["agent_id"])
    resp = await state["cancel"]
    assert resp.status_code == 200, resp.text
    assert view["status"] == "cancelled"
    assert h.reads.calls == []                      # the proposed call never ran
    [task] = await h.rows(AgentTask)
    assert task.status == "cancelled"


async def test_a_finished_run_cancels_to_itself(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    h.model.push(final("done"))
    view = await _run(h, alice, agent["agent_id"])
    resp = await _cancel(h, alice, agent["agent_id"], view["run_id"])
    assert resp.status_code == 200 and resp.json()["status"] == "completed"
    assert await _audit(h, "agent.run.cancelled") == []


async def test_another_user_cannot_stop_pause_or_resume(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    view = await _paused_run(h, alice, agent["agent_id"])
    assert (await _cancel(h, bob, agent["agent_id"], view["run_id"])).status_code == 404
    for verb in ("pause", "resume"):
        resp = await h.client.post(f"{API}/{agent['agent_id']}/{verb}", json={}, headers=bob.auth)
        assert resp.status_code == 404, (verb, resp.text)
    [run] = await h.rows(AgentRunRow)
    assert run.status == "waiting"
    [definition] = await h.rows(AgentDefinitionRow)
    assert definition.status == "active"


# ── pausing and resuming the agent ─────────────────────────────────────────


async def test_pausing_stops_live_runs_and_new_runs_without_confirmation(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    view = await _paused_run(h, alice, agent["agent_id"])
    resp = await h.client.post(f"{API}/{agent['agent_id']}/pause", json={}, headers=alice.auth)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "paused"
    [run] = await h.rows(AgentRunRow)
    assert run.status == "cancelled"
    refused = await run_agent(h, alice, agent["agent_id"])
    assert refused.status_code == 409 and refused.json()["error"]["details"]["reason"] == "paused"
    confirm = await h.confirm(alice, view["task"]["task_id"], view["task"]["pending"]["confirmation_token"])
    assert confirm.status_code in (404, 409)
    assert h.reads.calls == []
    assert len(await _audit(h, "agent.paused")) == 1


async def test_resuming_is_confirmed_by_the_owner_and_rechecked(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    await h.client.post(f"{API}/{agent['agent_id']}/pause", json={}, headers=alice.auth)
    first = await h.client.post(f"{API}/{agent['agent_id']}/resume", json={}, headers=alice.auth)
    assert first.status_code == 403, first.text
    token = first.json()["error"]["details"]["confirmation_token"]
    ok = await h.client.post(f"{API}/{agent['agent_id']}/resume", json={}, headers={**alice.auth, HEADER: token})
    assert ok.status_code == 200 and ok.json()["status"] == "active", ok.text
    h.model.push(final("running again"))
    assert (await _run(h, alice, agent["agent_id"]))["status"] == "completed"
    assert len(await _audit(h, "agent.resumed")) == 1


async def test_resume_cannot_bring_back_a_revoked_or_outdated_agent(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    await h.client.post(f"{API}/{agent['agent_id']}/pause", json={}, headers=alice.auth)
    # The template moved on while the agent was paused.
    import dataclasses
    service = h.app.state.agent_factory._factory.service
    registries = service.registries
    template = registries.templates["knowledge_keeper"]
    newer = {**registries.enabled_templates,
             "knowledge_keeper": template.model_copy(update={"version": template.version + 1})}
    service._registries = dataclasses.replace(registries, enabled_templates=newer)
    resp = await h.client.post(f"{API}/{agent['agent_id']}/resume", json={}, headers=alice.auth)
    assert resp.status_code == 409 and resp.json()["error"]["details"]["reason"] == "needs_reapproval"
    [definition] = await h.rows(AgentDefinitionRow)
    assert definition.status == "needs_reapproval"
    # And a revoked agent stays revoked.
    async with h.storage.session() as s:
        row = await s.get(AgentDefinitionRow, uuid.UUID(agent["agent_id"]))
        row.status = "revoked"
        await s.commit()
    resp = await h.client.post(f"{API}/{agent['agent_id']}/resume", json={}, headers=alice.auth)
    assert resp.status_code == 409 and resp.json()["error"]["details"]["reason"] == "agent_revoked"


async def test_pause_and_resume_are_idempotent_where_safe(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    for _ in range(2):
        resp = await h.client.post(f"{API}/{agent['agent_id']}/pause", json={}, headers=alice.auth)
        assert resp.status_code == 200 and resp.json()["status"] == "paused"
    # Resuming an active agent is a no-op that needs nothing.
    await h.client.post(f"{API}/{agent['agent_id']}/resume", json={}, headers=alice.auth)
    agent2 = await create_agent(h, alice, draft={**READ_DRAFT, "name": "Second reader"})
    resp = await h.client.post(f"{API}/{agent2['agent_id']}/resume", json={}, headers=alice.auth)
    assert resp.status_code == 200 and resp.json()["status"] == "active"


# ── deletion ───────────────────────────────────────────────────────────────


async def test_deleting_an_agent_stops_its_live_runs_and_leaves_no_route(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    view = await _paused_run(h, alice, agent["agent_id"])
    first = await h.client.delete(f"{API}/{agent['agent_id']}", headers=alice.auth)
    token = first.json()["error"]["details"]["confirmation_token"]
    gone = await h.client.delete(f"{API}/{agent['agent_id']}", headers={**alice.auth, HEADER: token})
    assert gone.status_code == 204, gone.text
    [run] = await h.rows(AgentRunRow)
    [task] = await h.rows(AgentTask)
    assert (run.status, task.status) == ("cancelled", "cancelled")
    confirm = await h.confirm(alice, view["task"]["task_id"], view["task"]["pending"]["confirmation_token"])
    assert confirm.status_code in (404, 409)
    assert h.reads.calls == []
    # Old run control is a safe 404, never a route into a deleted agent.
    assert (await _cancel(h, alice, agent["agent_id"], view["run_id"])).status_code == 404
    assert (await h.client.get(f"{API}/{agent['agent_id']}/runs", headers=alice.auth)).status_code == 404
    assert h.runtime._states.get(uuid.UUID(view["task"]["task_id"])) is None


async def test_a_delete_through_the_service_closes_runs_and_their_pauses_fail_closed(h):
    """The factory worker's `agent.delete.delete` reaches the service, not
    this facade: its live runs are closed in the store, so a paused one's
    confirmation fails its re-validation and nothing is performed."""

    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    view = await _paused_run(h, alice, agent["agent_id"])
    service = h.app.state.agent_factory._factory.service
    async with h.storage.session() as s:
        definition = await s.get(AgentDefinitionRow, uuid.UUID(agent["agent_id"]))
        await service.delete(s, definition)
        await s.commit()
    [run] = await h.rows(AgentRunRow)
    assert (run.status, run.failure_code) == ("cancelled", "deleted")
    await h.confirm(alice, view["task"]["task_id"], view["task"]["pending"]["confirmation_token"])
    [task] = await h.rows(AgentTask)
    assert (task.status, task.failure_code) == ("failed", "agent_unavailable")
    assert h.reads.calls == []


async def test_a_stop_holds_even_if_the_runtime_signal_were_lost(h, monkeypatch):
    """Defence in depth: the stop is recorded on the run itself, so even a
    task whose cancel signal never arrived fails its next step's
    re-validation — it cannot continue with hidden tool calls."""

    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    await h.grant(alice, "file.read", resource_scope=REPORTS)
    tasks = h.app.state.agent_tasks

    async def lost_signal(session, *, principal, task_id, audit):
        return None

    monkeypatch.setattr(tasks, "cancel", lost_signal)
    state: dict = {}

    async def cancel_mid_run(messages):
        [live] = [t for t in h.runtime._states.live() if t.agent is not None]
        state["resp"] = await _cancel(h, alice, agent["agent_id"], str(live.agent.run_id))
        return call("files.read", "list_directory", scope=REPORTS)

    h.model.push(ask("file.read", scope=REPORTS), cancel_mid_run, final("never reached"))
    view = await _run(h, alice, agent["agent_id"])
    assert state["resp"].status_code == 200
    assert view["status"] == "cancelled", view          # the run record: stopped by its owner
    [task] = await h.rows(AgentTask)
    assert (task.status, task.failure_code) == ("failed", "agent_unavailable")
    assert h.reads.calls == []                           # the proposed call never ran
