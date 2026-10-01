"""docs/29 §7.4, §15.1, §23.2 — on-demand, present-user agent runs (Phase 2 B).

A run is an ordinary task of the authenticated owner: the production runtime,
the production engine, a scripted worker. What must hold:

* the owner, graph, agent, version and hash come from the session and the
  stored, verified spec — never from the request;
* the task runs in the spec's mode, on the selected profile's model, under the
  spec's bounds, with the §9.7 framing (the owner's goal quoted as data);
* a deleted, revoked, tampered or foreign agent cannot run; neither can one
  from a graph the session is not in;
* the definition is re-validated at every step: an agent deleted or changed
  mid-run stops that run;
* with `agents.enabled: false` there is no run path at all.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import update

from server.storage.models import (
    AgentConfiguration,
    AgentDefinitionRow,
    AgentRunRow,
    AgentSpecVersionRow,
    AgentTask,
    AuditEvent,
)
from shared.schemas.enums import AgentConfigScopeType
from tests.agents.harness import AGENTS_ON, API, DRAFT, HEADER, create_agent, run_agent
from tests.runtime.conftest import call, final


@pytest.fixture
async def h(make_harness):
    return await make_harness(config=AGENTS_ON, agent_tools=True)


def run_view(resp) -> dict:
    assert resp.status_code == 202, resp.text
    return resp.json()


async def test_an_on_demand_run_is_an_ordinary_task_of_the_owner(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    h.model.push(final("Two critical advisories today."))
    view = run_view(await run_agent(h, alice, agent["agent_id"]))

    assert view["status"] == "completed" and view["agent_id"] == agent["agent_id"] and view["version"] == 1
    assert view["task"]["response"] == "Two critical advisories today."
    [run] = await h.rows(AgentRunRow)
    [task] = await h.rows(AgentTask, AgentTask.task_id == run.task_id)
    assert (task.user_id, task.device_id, task.mode) == (alice.user_id, alice.device_id, "observe")
    assert (run.owner_user_id, run.status, run.kind) == (alice.user_id, "completed", "on_demand")
    assert run.spec_hash == (await h.rows(AgentSpecVersionRow))[0].spec_hash
    # The worker saw the §9.7 framing, with the owner's goal quoted as data.
    prompt = h.model.all_text()
    assert "JARVIS agent run" in prompt and "they grant nothing" in prompt
    assert DRAFT["purpose"] in prompt
    started = await h.rows(AuditEvent, AuditEvent.action == "agent.run.started")
    finished = await h.rows(AuditEvent, AuditEvent.action == "agent.run.finished")
    assert len(started) == len(finished) == 1


async def test_the_run_uses_the_specs_bounds(make_harness):
    """The template's model-call bound (12) is tighter than the server's (16);
    the server's iteration bound is raised so it is the one that applies."""

    h = await make_harness(config={**AGENTS_ON, "agent": {"bounds": {"max_iterations": 40}}}, agent_tools=True)
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    h.model.push(*[call("no.such.tool", "x", args={"n": i}) for i in range(20)])
    view = run_view(await run_agent(h, alice, agent["agent_id"]))
    assert view["status"] == "failed" and view["failure_code"] == "max_model_calls_exceeded"
    [task] = await h.rows(AgentTask)
    assert task.model_calls == 12


async def test_a_request_cannot_name_another_owner_graph_version_or_hash(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    for body in ({"owner_user_id": str(uuid.uuid4())}, {"graph_id": str(uuid.uuid4())},
                 {"version": 2}, {"spec_hash": "0" * 64}, {"runtime_id": "letta"}, {"input": "do more"}):
        resp = await h.client.post(f"{API}/{agent['agent_id']}/runs", json=body, headers=alice.auth)
        assert resp.status_code == 422, (body, resp.text)
    assert await h.rows(AgentRunRow) == []


async def test_another_user_cannot_run_or_see_an_agents_runs(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    agent = await create_agent(h, alice)
    assert (await run_agent(h, bob, agent["agent_id"])).status_code == 404
    assert (await h.client.get(f"{API}/{agent['agent_id']}/runs", headers=bob.auth)).status_code == 404
    h.model.push(final("done"))
    run = run_view(await run_agent(h, alice, agent["agent_id"]))
    resp = await h.client.get(f"{API}/{agent['agent_id']}/runs/{run['run_id']}", headers=bob.auth)
    assert resp.status_code == 404
    assert (await run_agent(h, bob, str(uuid.uuid4()))).status_code == 404


async def test_a_deleted_agent_cannot_run(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    first = await h.client.delete(f"{API}/{agent['agent_id']}", headers=alice.auth)
    token = first.json()["error"]["details"]["confirmation_token"]
    await h.client.delete(f"{API}/{agent['agent_id']}", headers={**alice.auth, "X-Confirmation-Token": token})
    assert (await run_agent(h, alice, agent["agent_id"])).status_code == 404
    assert await h.rows(AgentRunRow) == []


async def test_a_tampered_spec_revokes_the_agent_and_refuses_the_run(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    async with h.storage.session() as s:
        row = (await s.get(AgentSpecVersionRow, (uuid.UUID(agent["agent_id"]), 1)))
        data = dict(row.spec_json)
        data["envelope_ceiling"] = [{"capability": "file.write", "operations": ["write_file"], "scope": {}}]
        await s.execute(update(AgentSpecVersionRow).values(spec_json=data))
        await s.commit()
    resp = await run_agent(h, alice, agent["agent_id"])
    assert resp.status_code == 409 and resp.json()["error"]["details"]["reason"] == "agent_revoked"
    [definition] = await h.rows(AgentDefinitionRow)
    assert definition.status == "revoked"
    assert await h.rows(AgentRunRow) == []


async def test_a_run_needs_the_agents_own_graph(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    graph = await h.shared_graph(alice, bob)
    agent = await create_agent(h, alice)
    [definition] = await h.rows(AgentDefinitionRow)
    assert definition.graph_id == graph
    other = await h.shared_graph(alice)            # alice now works in a different graph
    assert other != graph
    resp = await run_agent(h, alice, agent["agent_id"])
    assert resp.status_code == 409 and resp.json()["error"]["details"]["reason"] == "graph_mismatch"
    await h.enter_graph(alice, graph)
    h.model.push(final("ok"))
    assert run_view(await run_agent(h, alice, agent["agent_id"]))["status"] == "completed"


async def _mutate_definition(h, **values):
    async with h.storage.session() as s:
        await s.execute(update(AgentDefinitionRow).values(**values))
        await s.commit()


async def _update_agent(h, actor, agent_id: str) -> None:
    """A real, confirmed owner update to v2 (a new spec version and hash)."""

    compiled = await h.client.post(f"{API}/compile", params={"agent_id": agent_id},
                                   json={**DRAFT, "name": "Digest v2"}, headers=actor.auth)
    body = {"compile_id": compiled.json()["compile_id"]}
    first = await h.client.patch(f"{API}/{agent_id}", json=body, headers=actor.auth)
    token = first.json()["error"]["details"]["confirmation_token"]
    ok = await h.client.patch(f"{API}/{agent_id}", json=body, headers={**actor.auth, HEADER: token})
    assert ok.status_code == 200, ok.text


@pytest.mark.parametrize("change,code", [
    ({"status": "deleted"}, "agent_unavailable"),
    ({"status": "paused"}, "agent_unavailable"),
    ({"status": "revoked"}, "agent_unavailable"),
    # A version with no stored spec cannot verify: the agent is revoked.
    ({"current_version": 2}, "agent_unavailable"),
])
async def test_the_definition_is_revalidated_at_every_step(h, change, code):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)

    async def change_then_continue(messages):
        await _mutate_definition(h, **change)
        return call("no.such.tool", "x")

    h.model.push(change_then_continue, final("should never be reached"))
    view = run_view(await run_agent(h, alice, agent["agent_id"]))
    assert view["status"] == "failed" and view["failure_code"] == code
    [run] = await h.rows(AgentRunRow)
    assert run.status == "failed" and run.failure_code == code


async def test_an_owner_update_mid_run_stops_the_run_on_the_old_version(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)

    async def update_then_continue(messages):
        await _update_agent(h, alice, agent["agent_id"])
        return call("no.such.tool", "x")

    h.model.push(update_then_continue, final("should never be reached"))
    view = run_view(await run_agent(h, alice, agent["agent_id"]))
    assert view["status"] == "failed" and view["failure_code"] == "spec_changed"
    assert view["version"] == 1
    # The new version runs normally.
    h.model.push(final("v2 result"))
    again = run_view(await run_agent(h, alice, agent["agent_id"]))
    assert (again["status"], again["version"]) == ("completed", 2)


async def test_runs_are_listed_and_read_by_their_owner(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    h.model.push(final("one"), final("two"))
    first = run_view(await run_agent(h, alice, agent["agent_id"]))
    second = run_view(await run_agent(h, alice, agent["agent_id"]))
    listed = (await h.client.get(f"{API}/{agent['agent_id']}/runs", headers=alice.auth)).json()["items"]
    assert {r["run_id"] for r in listed} == {first["run_id"], second["run_id"]}
    one = (await h.client.get(f"{API}/{agent['agent_id']}/runs/{first['run_id']}", headers=alice.auth)).json()
    assert (one["status"], one["task_id"]) == ("completed", first["task_id"])
    # A run id under another of the owner's agents is not found there.
    other = await create_agent(h, alice, draft={**DRAFT, "name": "Other digest"})
    resp = await h.client.get(f"{API}/{other['agent_id']}/runs/{first['run_id']}", headers=alice.auth)
    assert resp.status_code == 404


async def test_disabled_there_is_no_run_path(make_harness):
    h = await make_harness()
    alice = await h.user("alice")
    resp = await run_agent(h, alice, str(uuid.uuid4()))
    assert resp.status_code == 503 and resp.json()["error"]["details"]["dependency"] == "agents"
    assert (await h.client.get(f"{API}/{uuid.uuid4()}/runs", headers=alice.auth)).status_code == 503


async def test_an_ordinary_task_is_unchanged_with_agents_enabled(h):
    alice = await h.user("alice")
    h.model.push(final("plain answer"))
    resp = await h.submit(alice, "hello")
    assert resp.status_code == 200 and resp.json()["response"] == "plain answer"
    assert await h.rows(AgentRunRow) == []
    [task] = await h.rows(AgentTask)
    assert task.mode == "execute"


async def test_a_run_refused_before_its_task_starts_is_recorded_failed(make_harness, monkeypatch):
    """18 §5.4: while the operator's global latch is set no task is created —
    an agent run included. The run record says it never started."""

    from server.security.superuser import SUPERUSER_TOKEN_ENV

    token = "TEST-ONLY-superuser-credential-0123456789abcdef"
    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, token)
    h = await make_harness(config=AGENTS_ON, agent_tools=True)
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    stop = await h.client.post("/api/v1/admin/control/global-stop", json={"reason": "incident"},
                               headers={"Authorization": f"Superuser {token}"})
    assert stop.status_code == 200, stop.text
    resp = await run_agent(h, alice, agent["agent_id"])
    assert resp.status_code == 503 and resp.json()["error"]["details"]["dependency"] == "supervisor"
    [run] = await h.rows(AgentRunRow)
    assert (run.status, run.failure_code, run.task_id) == ("failed", "not_started", None)
    assert await h.rows(AgentTask) == []


def _swap_registries(h, **changes) -> None:
    """Simulate a later deployment's registries (a server restart with a new
    template version or a withdrawn profile) under the same stored agent."""

    import dataclasses

    service = h.app.state.agent_factory._factory.service
    service._registries = dataclasses.replace(service.registries, **changes)


async def test_a_run_on_an_outdated_template_needs_reapproval(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    registries = h.app.state.agent_factory._factory.service.registries
    template = registries.templates["research_digest"]
    newer = {**registries.enabled_templates,
             "research_digest": template.model_copy(update={"version": template.version + 1})}
    _swap_registries(h, enabled_templates=newer)
    resp = await run_agent(h, alice, agent["agent_id"])
    assert resp.status_code == 409 and resp.json()["error"]["details"]["reason"] == "needs_reapproval"
    [definition] = await h.rows(AgentDefinitionRow)
    assert definition.status == "needs_reapproval"
    assert await h.rows(AgentRunRow) == []
    # And it stays refused, as an inactive agent.
    again = await run_agent(h, alice, agent["agent_id"])
    assert again.status_code == 409 and again.json()["error"]["details"]["reason"] == "needs_reapproval"


def _profiles(h, change: str) -> dict:
    import dataclasses

    registries = h.app.state.agent_factory._factory.service.registries
    if change == "removed":
        return {}
    resolved = registries.model_profiles["general-agentic"]
    update = {"enabled": False} if change == "disabled" else {"version": resolved.profile.version + 1}
    return {"general-agentic": dataclasses.replace(resolved, profile=resolved.profile.model_copy(update=update))}


@pytest.mark.parametrize("change", ["removed", "disabled", "new_version"])
async def test_a_run_needs_its_profile_still_configured_and_current(h, change):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    # A later deployment no longer offers the profile the agent was built on.
    _swap_registries(h, model_profiles=_profiles(h, change))
    resp = await run_agent(h, alice, agent["agent_id"])
    assert resp.status_code == 409 and resp.json()["error"]["details"]["reason"] == "model_profile_unavailable"
    assert await h.rows(AgentRunRow) == []
    refused = await h.rows(AuditEvent, AuditEvent.action == "agent.run.refused")
    assert len(refused) == 1 and refused[0].resource.endswith(":model_profile_unavailable")


async def test_a_run_needs_the_profile_still_permitted_to_its_owner(h):
    """Withdrawn from "open to all", the profile is usable only by an owner
    whose own primary model it is — and alice now has a model of her own."""

    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    _swap_registries(h, open_to_all=frozenset())
    h.model.push(final("still mine"))
    # Her primary is still the server's `agent.primary`: the profile's entry.
    assert run_view(await run_agent(h, alice, agent["agent_id"]))["status"] == "completed"
    async with h.storage.session() as s:
        s.add(AgentConfiguration(
            scope_type=AgentConfigScopeType.USER, scope_id=alice.user_id,
            primary_model={"provider": "ollama", "model": "her-own-model", "endpoint": "http://127.0.0.1:11434"},
            updated_at=datetime.now(timezone.utc),
        ))
        await s.commit()
    resp = await run_agent(h, alice, agent["agent_id"])
    assert resp.status_code == 409 and resp.json()["error"]["details"]["reason"] == "model_profile_unavailable"


@pytest.mark.parametrize("status", ["paused", "needs_reapproval"])
async def test_an_inactive_agent_cannot_start_a_run(h, status):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    await _mutate_definition(h, status=status)
    resp = await run_agent(h, alice, agent["agent_id"])
    assert resp.status_code == 409 and resp.json()["error"]["details"]["reason"] == status
    assert await h.rows(AgentRunRow) == [] and await h.rows(AgentTask) == []
