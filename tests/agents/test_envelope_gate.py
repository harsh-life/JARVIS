"""docs/29 §10.3 — the envelope gate, wired into a real agent run (Phase 2 C).

The gate sits in the runtime *before* the existing path and can only remove:

    agent proposal → envelope gate → activation → 04 (D1–D5, floor, tier)
      → confirmation → execution

What must hold, end to end through the production runtime and engine:

* a capability, operation or scope outside the compiled envelope is refused
  as an observation and never offered for confirmation — even when the owner
  holds a standing grant for it;
* inside the envelope nothing is granted: the owner's own grant (or their
  confirmation of a task-scoped one) is still required, and a grant revoked
  mid-run stops the next call — decisions are never cached;
* `agent.*`, `system.restricted` and device control are unreachable;
* the worker is shown only the tools its envelope reaches;
* an approval given at a pause is re-checked against the envelope.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from server.agent.envelope import Envelope
from server.storage.models import AgentRunRow, AuditEvent, ConfirmationToken
from shared.schemas.agent_factory import EnvelopeEntry
from tests.agents.harness import AGENTS_ON, create_agent, run_agent
from tests.runtime.conftest import ask, call, final

REPORTS = {"sandbox_root": "reports"}

FILE_DRAFT = {
    "name": "Report tidier",
    "purpose": "Tidy the files in my reports folder.",
    "task_tags": ["document_processing"],
    "requested_abilities": ["read_sandbox_files", "write_sandbox_files"],
    "sources": [{"kind": "sandbox_path", "value": "reports"}],
}

READ_DRAFT = {
    "name": "Report reader",
    "purpose": "Summarize the files in my reports folder.",
    "task_tags": ["knowledge_maintenance"],
    "requested_abilities": ["read_sandbox_files"],
    "sources": [{"kind": "sandbox_path", "value": "reports"}],
}


@pytest.fixture
async def h(make_harness):
    return await make_harness(config=AGENTS_ON, agent_tools=True)


def observed(h) -> str:
    return h.model.all_text()


async def _run(h, actor, agent_id: str) -> dict:
    resp = await run_agent(h, actor, agent_id)
    assert resp.status_code == 202, resp.text
    return resp.json()


async def _denials(h) -> list[AuditEvent]:
    return await h.rows(AuditEvent, AuditEvent.action == "agent.envelope.denied")


async def test_inside_the_envelope_the_existing_path_still_decides(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    await h.grant(alice, "file.read", resource_scope=REPORTS)
    h.model.push(ask("file.read", scope=REPORTS), call("files.read", "list_directory", scope=REPORTS),
                 final("three reports"))
    view = await _run(h, alice, agent["agent_id"])
    assert view["status"] == "completed", view
    assert len(h.reads.calls) == 1
    assert await _denials(h) == []


async def test_the_envelope_never_grants_the_owner_must_still_approve(h):
    """No standing grant: the in-envelope activation pauses for the owner's
    confirmation exactly as it would for any task — the spec granted nothing."""

    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    h.model.push(ask("file.read", scope=REPORTS), final("never reached"))
    view = await _run(h, alice, agent["agent_id"])
    assert view["status"] == "waiting" and view["task"]["status"] == "awaiting_confirmation"
    assert view["task"]["pending"]["capability"] == "file.read"
    assert h.reads.calls == []


@pytest.mark.parametrize("capability,scope", [
    ("file.write", REPORTS),                 # not in this agent's envelope
    ("app.interact", {"package_name": "com.example"}),
    ("system.restricted", None),
    ("agent.define", None),
    ("agent.delete", None),
    ("file.read", {"sandbox_root": "private"}),   # outside the entry's scope
    ("file.read", None),                          # unscoped: wider than the entry
])
async def test_outside_the_envelope_is_refused_and_never_offered(h, capability, scope):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    # Even a standing grant of the owner's does not reach past the ceiling.
    if capability in {"file.write", "file.read", "app.interact"}:
        await h.grant(alice, capability, resource_scope=scope)
    h.model.push(ask(capability, scope=scope), final("gave up"))
    before = len(await h.rows(ConfirmationToken))
    view = await _run(h, alice, agent["agent_id"])
    assert view["status"] == "completed" and view["task"]["active_capabilities"] == []
    assert "outside this agent's approved abilities" in observed(h)
    assert len(await h.rows(ConfirmationToken)) == before  # nothing was offered
    [denied] = await _denials(h)
    assert denied.resource == f"capability:{capability}"


async def test_an_operation_outside_the_entry_is_refused_before_the_engine(h):
    """file_organizer's envelope has file.write {create_file, write_file}:
    `delete_file` (which would otherwise ask the owner) is never offered."""

    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=FILE_DRAFT)
    await h.grant(alice, "file.write", resource_scope=REPORTS)
    h.model.push(ask("file.write", scope=REPORTS),
                 call("files.write", "delete_file", ref="x.txt", scope=REPORTS),
                 call("files.write", "bulk_delete", scope=REPORTS),
                 final("could not"))
    before = len(await h.rows(ConfirmationToken))
    view = await _run(h, alice, agent["agent_id"])
    assert view["status"] == "completed"
    assert h.writes.calls == []
    assert len(await h.rows(ConfirmationToken)) == before  # nothing was offered
    assert {d.resource for d in await _denials(h)} == {"tool:files.write.delete_file", "tool:files.write.bulk_delete"}


async def test_an_in_envelope_write_still_goes_through_the_engine(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=FILE_DRAFT)
    await h.grant(alice, "file.write", resource_scope=REPORTS)
    h.model.push(ask("file.write", scope=REPORTS),
                 call("files.write", "create_file", args={"relative_path": "index.md"}, scope=REPORTS),
                 final("created"))
    view = await _run(h, alice, agent["agent_id"])
    assert view["status"] == "completed", view
    assert len(h.writes.calls) == 1


async def test_a_grant_revoked_mid_run_stops_the_next_call(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    grant = await h.grant(alice, "file.read", resource_scope=REPORTS)

    async def revoke_then_list(messages):
        await h.revoke(alice, grant)
        return call("files.read", "list_directory", scope=REPORTS)

    h.model.push(ask("file.read", scope=REPORTS), call("files.read", "list_directory", scope=REPORTS),
                 revoke_then_list, final("done"))
    view = await _run(h, alice, agent["agent_id"])
    assert view["status"] == "completed"
    assert len(h.reads.calls) == 1   # the second list never ran
    assert "Not permitted" in observed(h)


async def test_the_worker_sees_only_the_tools_its_envelope_reaches(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    h.model.push(final("ok"))
    await _run(h, alice, agent["agent_id"])
    system = h.model.seen[0][0].content
    assert "files.read" in system
    for hidden in ("files.write", "ui.app", "agents.define", "agents.inspect", "agents.delete"):
        assert hidden not in system, hidden


async def test_an_approval_is_rechecked_against_the_envelope(h):
    """The pause survives, but the ceiling at approval time is what counts:
    approving an activation never carries it past the envelope. (In v1 every
    in-envelope tool operation is low-tier and automatic, so an activation is
    the only pause an agent run can reach.)"""

    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    h.model.push(ask("file.read", scope=REPORTS), call("files.read", "list_directory", scope=REPORTS),
                 final("done"))
    view = await _run(h, alice, agent["agent_id"])
    assert view["status"] == "waiting", view
    task_id = view["task"]["task_id"]
    state = h.runtime._states.get(uuid.UUID(task_id))
    narrowed = Envelope(entries=(EnvelopeEntry(capability="file.read", operations=("list_directory",),
                                               scope={"sandbox_root": "elsewhere"}),),
                        risk_ceiling=state.agent.envelope.risk_ceiling, spec_hash=state.agent.spec_hash)
    state.agent = dataclasses.replace(state.agent, envelope=narrowed)
    resp = await h.confirm(alice, task_id, view["task"]["pending"]["confirmation_token"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["active_capabilities"] == []
    assert h.reads.calls == []
    assert "outside this agent's approved abilities" in observed(h)
    [run] = await h.rows(AgentRunRow)
    assert run.status == "completed"


async def test_an_owner_removed_from_the_agents_graph_mid_run_stops_the_run(h):
    """04 §9: graph membership is re-checked live at every step of the run."""

    alice, bob = await h.user("alice"), await h.user("bob")
    graph = await h.shared_graph(alice, bob)
    agent = await create_agent(h, bob, draft=READ_DRAFT)

    async def remove_bob(messages):
        r = await h.client.delete(f"/api/v1/graphs/{graph}/members/{bob.user_id}", headers=alice.auth)
        assert r.status_code == 204, r.text
        return call("files.read", "list_directory", scope=REPORTS)

    h.model.push(remove_bob, final("never reached"))
    view = await _run(h, bob, agent["agent_id"])
    assert view["status"] == "failed" and view["failure_code"] == "agent_unavailable"
    assert h.reads.calls == []
