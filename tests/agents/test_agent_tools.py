"""docs/29 §2.1, §23.1 — the factory worker's tool path, end to end.

Nothing security-relevant is stubbed: the production composition root, real
OIDC → device → token onboarding, the real runtime, the real engine, real
confirmation tokens. Only the worker model is scripted — so a test controls
exactly what it proposes, adversarial proposals included.

What must hold:
* creating an agent needs `agent.define` activated **and** a consequential,
  single-use confirmation of the compiled spec (AGENT-T1);
* the draft grants nothing: a draft naming authority is rejected whole and
  audited by field name only (AGENT-T2);
* a non-execute task can compile and inspect but never create;
* another user's agent is `not found` to every operation (AGENT-T9);
* previews are task-bound;
* with `agents.enabled: false` nothing of this exists (the existing system is
  unchanged).
"""

from __future__ import annotations

import pytest

from server.security.events import AuditAction
from server.storage.models import AgentCompilePreviewRow, AgentDefinitionRow, AgentSpecVersionRow, AuditEvent
from tests.agents.harness import AGENTS_ON, DRAFT, call_json, last_agent_id, last_compile_id
from tests.runtime.conftest import ask, final, pending_of

DEFINE, INSPECT, DELETE = "agent.define", "agent.inspect", "agent.delete"


@pytest.fixture
async def h(make_harness):
    return await make_harness(config=AGENTS_ON, agent_tools=True)


def compile_step(draft=None) -> str:
    return call_json(DEFINE, "compile", draft or DRAFT)


def create_step(messages) -> str:
    return call_json(DEFINE, "create", {"compile_id": last_compile_id(messages)})


def completed(resp) -> dict:
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed", resp.json()
    return resp.json()


async def _create_agent(h, actor) -> str:
    await h.grant(actor, DEFINE)
    h.model.push(ask(DEFINE), compile_step(), create_step)
    paused = pending_of(await h.submit(actor, "make me an advisory agent"))
    h.model.push(final("Created."))
    done = await h.confirm(actor, paused["task_id"], paused["pending"]["confirmation_token"])
    assert done.status_code == 200, done.text
    [row] = await h.rows(AgentDefinitionRow, AgentDefinitionRow.owner_user_id == actor.user_id)
    return str(row.agent_id)


# ── AGENT-T1 ─────────────────────────────────────────────────────────────


async def test_agent_t1_creation_needs_activation_and_a_confirmed_spec(h):
    alice = await h.user("alice")
    await h.grant(alice, DEFINE)
    h.model.push(ask(DEFINE), compile_step(), create_step)
    paused = pending_of(await h.submit(alice, "Check my advisories every morning and summarize them"))

    pending = paused["pending"]
    assert (pending["tool_id"], pending["operation"], pending["risk_category"]) == (DEFINE, "create", "consequential")
    assert set(pending["arguments"]) == {"compile_id"}
    # Nothing exists until the owner approves.
    assert await h.rows(AgentDefinitionRow) == []
    assert len(await h.rows(AgentCompilePreviewRow)) == 1
    # The compile observation carried the deterministic card, not worker prose.
    assert "Security advisory digest" in h.model.all_text() and "create agents" in h.model.all_text()

    h.model.push(final("Your agent is ready."))
    done = await h.confirm(alice, paused["task_id"], pending["confirmation_token"])
    assert done.status_code == 200, done.text and done.json()["status"] == "completed"
    [row] = await h.rows(AgentDefinitionRow)
    assert (row.owner_user_id, row.status, row.current_version) == (alice.user_id, "active", 1)
    assert str(row.created_from_task_id) == paused["task_id"] and row.created_by_device_id == alice.device_id
    assert len(await h.rows(AgentSpecVersionRow)) == 1
    created = await h.rows(AuditEvent, AuditEvent.action == AuditAction.AGENT_CREATED.value)
    assert [e.actor.value for e in created] == ["agent"]


async def test_without_a_grant_activating_agent_define_asks_the_user(h):
    alice = await h.user("alice")
    h.model.push(ask(DEFINE), compile_step())
    paused = pending_of(await h.submit(alice, "make me an agent"))
    assert paused["pending"]["capability"] == DEFINE and paused["pending"]["kind"] == "capability_activation"
    assert await h.rows(AgentCompilePreviewRow) == []


async def test_declining_the_confirmation_creates_nothing(h):
    alice = await h.user("alice")
    await h.grant(alice, DEFINE)
    h.model.push(ask(DEFINE), compile_step(), create_step)
    paused = pending_of(await h.submit(alice, "make me an agent"))
    h.model.push(final("ok"))
    await h.confirm(alice, paused["task_id"], paused["pending"]["confirmation_token"], approve=False)
    assert await h.rows(AgentDefinitionRow) == []


# ── AGENT-T2: the draft grants nothing ───────────────────────────────────


async def test_agent_t2_a_draft_naming_authority_is_rejected_and_audited_by_name(h):
    alice = await h.user("alice")
    await h.grant(alice, DEFINE)
    leaked = "sk-" + "attacker-controlled-value"
    h.model.push(ask(DEFINE), compile_step({**DRAFT, "capabilities": ["system.restricted"], "secret_ref": leaked}),
                 final("could not"))
    completed(await h.submit(alice, "make me an agent"))
    assert await h.rows(AgentCompilePreviewRow) == []
    rejected = await h.rows(AuditEvent, AuditEvent.action == AuditAction.AGENT_DRAFT_REJECTED.value)
    assert len(rejected) == 1
    assert "capabilities" in rejected[0].resource and "secret_ref" in rejected[0].resource
    assert leaked not in rejected[0].resource
    assert "draft_invalid" in h.model.all_text()


async def test_the_worker_cannot_author_the_create_arguments(h):
    alice = await h.user("alice")
    await h.grant(alice, DEFINE)
    h.model.push(ask(DEFINE), compile_step(),
                 lambda m: call_json(DEFINE, "create", {"compile_id": last_compile_id(m), "owner_user_id": "x"}))
    paused = pending_of(await h.submit(alice, "make me an agent"))
    h.model.push(final("no"))
    await h.confirm(alice, paused["task_id"], paused["pending"]["confirmation_token"])
    assert await h.rows(AgentDefinitionRow) == []
    assert "invalid_arguments" in h.model.all_text()


# ── modes ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["observe", "draft", "suggest"])
async def test_a_non_execute_task_compiles_but_never_creates(h, mode):
    alice = await h.user("alice")
    await h.grant(alice, DEFINE)
    h.model.push(ask(DEFINE), compile_step(), create_step, final("done"))
    completed(await h.submit(alice, "make me an agent", extra_body={"mode": mode}))
    assert await h.rows(AgentDefinitionRow) == []


# ── AGENT-T9 and binding ─────────────────────────────────────────────────


async def test_agent_t9_another_users_agent_does_not_exist_for_them(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    agent_id = await _create_agent(h, alice)
    await h.grant(bob, INSPECT)
    await h.grant(bob, DELETE)
    await h.grant(bob, DEFINE)
    h.model.push(ask(INSPECT, DELETE, DEFINE),
                 call_json(INSPECT, "get", {}, ref=agent_id),
                 call_json(DELETE, "delete", {}, ref=agent_id),
                 call_json(DEFINE, "compile_update", DRAFT, ref=agent_id),
                 call_json(INSPECT, "list", {}),
                 final("nothing"))
    body = completed(await h.submit(bob, "look at agents"))   # never paused: no confirmation offered
    assert body["status"] == "completed"
    text = h.model.all_text()
    assert text.count("Not found or not permitted.") >= 3
    # Nothing of Alice's agent (its template, its card) reached Bob's task.
    bobs = text.split("look at agents", 1)[1]
    assert "research_digest" not in bobs and "your JARVIS inbox only" not in bobs
    [row] = await h.rows(AgentDefinitionRow)
    assert row.status == "active"


async def test_a_preview_is_bound_to_the_task_that_compiled_it(h):
    alice = await h.user("alice")
    await h.grant(alice, DEFINE)
    h.model.push(ask(DEFINE), compile_step(), final("compiled"))
    completed(await h.submit(alice, "compile only"))
    compile_id = last_compile_id(h.model.seen[-1])
    h.model.push(ask(DEFINE), call_json(DEFINE, "create", {"compile_id": compile_id}))
    paused = pending_of(await h.submit(alice, "now create it"))
    h.model.push(final("hmm"))
    await h.confirm(alice, paused["task_id"], paused["pending"]["confirmation_token"])
    assert await h.rows(AgentDefinitionRow) == []
    assert "preview_not_found" in h.model.all_text()


async def test_inspect_lists_only_the_owners_agents(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    await _create_agent(h, alice)
    await h.grant(bob, INSPECT)
    h.model.push(ask(INSPECT), call_json(INSPECT, "list", {}), final("listed"))
    completed(await h.submit(bob, "list my agents"))
    assert '"items": []' in h.model.all_text() or '"items":[]' in h.model.all_text()


# ── update and delete through the worker ─────────────────────────────────


async def test_update_is_a_confirmed_new_version(h):
    alice = await h.user("alice")
    agent_id = await _create_agent(h, alice)
    h.model.push(ask(DEFINE), call_json(DEFINE, "compile_update", {**DRAFT, "name": "Digest v2"}, ref=agent_id),
                 lambda m: call_json(DEFINE, "update", {"compile_id": last_compile_id(m)}, ref=agent_id))
    paused = pending_of(await h.submit(alice, "rename my agent"))
    assert paused["pending"]["operation"] == "update" and paused["pending"]["resource_ref"] == agent_id
    h.model.push(final("renamed"))
    await h.confirm(alice, paused["task_id"], paused["pending"]["confirmation_token"])
    [row] = await h.rows(AgentDefinitionRow)
    assert (row.current_version, row.name) == (2, "Digest v2")


async def test_delete_is_confirmed_and_leaves_a_tombstone(h):
    alice = await h.user("alice")
    agent_id = await _create_agent(h, alice)
    await h.grant(alice, DELETE)
    h.model.push(ask(DELETE), call_json(DELETE, "delete", {}, ref=agent_id))
    paused = pending_of(await h.submit(alice, "delete my agent"))
    assert paused["pending"]["risk_category"] == "consequential"
    h.model.push(final("deleted"))
    await h.confirm(alice, paused["task_id"], paused["pending"]["confirmation_token"])
    [row] = await h.rows(AgentDefinitionRow)
    assert row.status == "deleted" and row.name is None
    assert await h.rows(AgentSpecVersionRow) == []


async def test_revoking_the_grant_stops_the_next_use(h):
    alice = await h.user("alice")
    grant_id = await h.grant(alice, DEFINE)
    h.model.push(ask(DEFINE), compile_step(), final("ok"))
    completed(await h.submit(alice, "compile"))
    await h.revoke(alice, grant_id)
    before = len(await h.rows(AgentCompilePreviewRow))
    h.model.push(ask(DEFINE), compile_step())
    paused = pending_of(await h.submit(alice, "compile again"))   # asks the user again; nothing ran
    assert paused["pending"]["kind"] == "capability_activation"
    assert len(await h.rows(AgentCompilePreviewRow)) == before


# ── agents.enabled: false ────────────────────────────────────────────────


async def test_disabled_the_factory_does_not_exist(make_harness):
    h = await make_harness(agent_tools=True)
    alice = await h.user("alice")
    await h.grant(alice, DEFINE)
    h.model.push(ask(DEFINE), compile_step(), final("no such tool"))
    completed(await h.submit(alice, "make me an agent"))
    text = h.model.all_text()
    assert "Tool 'agent.define' is not available." in text
    tools = (await h.client.get("/api/v1/config/tools", headers=alice.auth)).json()["items"]
    assert tools and not [t for t in tools if t["tool_id"].startswith("agent.")]
    assert "agent.define" not in h.model.seen[0][0].content   # the worker's system prompt
    resp = await h.client.get("/api/v1/agents", headers=alice.auth)
    assert resp.status_code == 503 and resp.json()["error"]["details"]["dependency"] == "agents"
