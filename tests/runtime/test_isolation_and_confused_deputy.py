"""RT-T5, 05 §8 — confused-deputy prevention: the agent acting for principal
A can never do, on A's behalf, anything A could not do directly — and never
anything for a *different* principal at all.
"""

from __future__ import annotations

import json

import pytest

from server.agent.orchestrator import AgentOrchestrator
from server.agent.tasks import TaskStore, UnknownTask
from server.memory.hydrator import NullMemoryHydrator
from shared.schemas.enums import CapabilityScopeType, GraphType
from shared.schemas.runtime import AgentProposal as RuntimeAgentProposal
from shared.schemas.runtime import TaskStatus

from tests.runtime.conftest import (
    RecordingToolExecutor,
    ScriptedModelInvoker,
    build_event_recorder,
    build_registry,
    build_tool_catalog,
    build_tool_dispatcher,
    principal_for,
)

def _tool_call(**kw):
    payload = {"kind": "tool_call", "tool_id": "notes", "capability": "file.write", "operation": "write_file"}
    payload.update(kw)
    return json.dumps(payload)


def _final(text="done"):
    return json.dumps({"kind": "final_answer", "final_text": text})


def _make(principal, authorizer, db, audit, bounds, model_responses, task_store=None):
    executor = RecordingToolExecutor()
    registry = build_registry(tool_id="notes", capability="file.write", executor=executor)
    orch = AgentOrchestrator(
        principal=principal,
        bounds=bounds,
        authorizer=authorizer,
        model=ScriptedModelInvoker(responses=list(model_responses)),
        tools=build_tool_dispatcher(registry=registry, allowed_tool_ids=frozenset({"notes"}), db=db, audit=audit, principal=principal),
        memory=NullMemoryHydrator(),
        events=build_event_recorder(audit=audit, principal=principal),
        tool_catalog=build_tool_catalog(registry=registry, allowed_tool_ids=frozenset({"notes"})),
        task_store=task_store or TaskStore(),
    )
    return orch, executor


def test_agent_proposal_schema_has_no_identity_or_authority_fields():
    """Structural proof, not just discipline: `AgentProposal` has no field
    through which a model could assert a user/device/session identity, a
    role, or a pre-granted capability — the runtime cannot 'trust' a field
    that does not exist."""

    field_names = set(RuntimeAgentProposal.model_fields)
    forbidden = {"user_id", "device_id", "session_id", "role", "principal", "granted", "approved", "authorized"}
    assert field_names.isdisjoint(forbidden)


async def test_a_graph_id_claimed_by_a_non_member_is_denied_never_a_grant(
    alice, bob, authorizer, db, audit, small_bounds, grants, graph_service
):
    """04 §0: "a `graph_id` in a request body is a claim to check, never a
    grant." Bob's model proposes acting inside a graph he never joined —
    membership (D1) still denies it, exactly as if bob had made the HTTP
    call directly."""

    graph = await graph_service.create_graph(
        db, name="alice's graph", graph_type=GraphType.SHARED, creator_user_id=alice.user_id, audit=audit
    )
    await grants.grant(
        db, principal_id=bob.user_id, scope_type=CapabilityScopeType.USER,
        capability="file.write", granted_by=bob.user_id,
    )
    bob_principal = principal_for(bob)  # bob is not a member of alice's graph
    orch, executor = _make(
        bob_principal, authorizer, db, audit, small_bounds,
        [_tool_call(graph_id=str(graph.graph_id)), _final("could not act in that graph")],
    )

    result = await orch.start("write a note in alice's graph")

    assert result.status is TaskStatus.COMPLETED
    assert executor.requests == []  # the claimed graph_id granted nothing


async def test_a_task_created_for_one_principal_is_invisible_to_another(
    alice, bob, authorizer, db, audit, small_bounds
):
    """A task_id is not a capability — another user's task is reported
    absent (04 §7's anti-enumeration posture), never merely forbidden."""

    task_store = TaskStore()
    alice_principal = principal_for(alice)
    alice_orch, _ = _make(alice_principal, authorizer, db, audit, small_bounds, [_final("alice's answer")], task_store=task_store)
    alice_result = await alice_orch.start("alice's request")
    assert alice_result.status is TaskStatus.COMPLETED

    bob_principal = principal_for(bob)
    bob_orch, _ = _make(bob_principal, authorizer, db, audit, small_bounds, [], task_store=task_store)

    with pytest.raises(UnknownTask):
        await bob_orch.get_status(alice_result.task_id)


async def test_a_task_cannot_be_cancelled_by_a_different_principal(alice, bob, small_bounds):
    task_store = TaskStore()
    alice_principal = principal_for(alice)
    state = await task_store.create(principal=alice_principal)

    bob_principal = principal_for(bob)
    with pytest.raises(UnknownTask):
        await task_store.request_cancellation(state.task_id, requested_by=bob_principal)


async def test_the_runtime_never_reads_identity_from_the_proposal(alice, authorizer, db, audit, small_bounds, grants, bob):
    """Even if a proposal *could* somehow carry another user's id (it
    cannot — see the schema test above), `_build_access_request` only ever
    reads `self._principal`, which is fixed at orchestrator construction
    from the authenticated caller. This test proves the observable
    consequence: granting the capability to *bob* does not let *alice's*
    orchestrator use it, even though both share the exact same proposal."""

    await grants.grant(
        db, principal_id=bob.user_id, scope_type=CapabilityScopeType.USER,
        capability="file.write", granted_by=bob.user_id,
    )
    alice_principal = principal_for(alice)  # alice has no grant
    orch, executor = _make(alice_principal, authorizer, db, audit, small_bounds, [_tool_call(), _final("denied")])

    result = await orch.start("write a note")

    assert result.status is TaskStatus.COMPLETED
    assert executor.requests == []
