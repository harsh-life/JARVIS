"""BR-T2, the unattended-agent dimension (docs/OD_A1_BR_T2.md §3f; docs/29
§15, Phase 5).

Phase 5 adds a second principal form — an unattended run's
`DelegatedPrincipal` (owner, agent, delegation, run; no device, no session) —
and a server-driven way to start a run. This module measures what that adds,
on the production composition root, with the same attacker models as the
other BR-T2 tables:

* **authorized** — another user driving their own agent through every path
  the deterministic layer allows. A reachable row here is a cross-user
  authorization failure, never an accepted residual.
* **app-RCE** — code running inside the server process (OD-A1 (a)).

Asserted in both directions (INV-20): contained rows must stay contained and
reachable rows must stay reachable; a change in either is a re-measurement.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from server.graph.authorization import AccessRequest
from server.security.audit import AuditLogger
from shared.schemas.authorization import DelegatedPrincipal, Operation, ResourceType
from shared.schemas.enums import Visibility
from tests.agents.harness import UNATTENDED_ON
from tests.agents.test_unattended_runs import _occurrences, _runs, _setup, _tick
from tests.runtime.conftest import ask, call, final


@pytest.fixture
async def h(make_harness):
    return await make_harness(config={**UNATTENDED_ON, "android": {"enabled": True,
                                                                  "app_classification": {
                                                                      "non_sensitive": ["com.example"]}}})


async def _decide(h, principal, **request):
    async with h.storage.session() as s:
        outcome = await h.core.engine.authorize(s, AccessRequest(principal=principal, **request),
                                                audit=AuditLogger(s, request_id=uuid.uuid4()))
        await s.commit()
    return outcome


async def test_row_42_bs_unattended_run_cannot_read_as_private_file(h):
    """authorized → contained: the engine decides B's delegated principal as
    B (D4), so A's private file is `404` to it like to B."""

    alice, bob = await h.user("alice"), await h.user("bob")
    secret = await h.file(alice, None, Visibility.PRIVATE, "a-private.txt")
    agent, row = await _setup(h, bob)
    principal = DelegatedPrincipal(user_id=bob.user_id, agent_id=row.agent_id, delegation_id=row.delegation_id,
                                   run_id=uuid.uuid4(), graph_id=row.graph_id)
    outcome = await _decide(h, principal, operation=Operation.READ, resource_type=ResourceType.FILERESOURCE,
                            resource_ref=secret, required_capability="file.read", capability_operation="read_file")
    assert not outcome.allowed and outcome.surface.value == "not_found"
    # The owner's own file, by contrast, is decided on the owner's live grant.
    own = await h.file(bob, None, Visibility.PRIVATE, "b.txt")
    assert not (await _decide(h, principal, operation=Operation.READ, resource_type=ResourceType.FILERESOURCE,
                              resource_ref=own, required_capability="file.read",
                              capability_operation="read_file")).allowed   # no grant: still nothing
    await h.grant(bob, "file.read")
    assert (await _decide(h, principal, operation=Operation.READ, resource_type=ResourceType.FILERESOURCE,
                          resource_ref=own, required_capability="file.read", capability_operation="read_file")).allowed


async def test_row_43_an_unattended_run_reaches_no_phone(h, monkeypatch):
    """authorized → contained: there is no device on the principal to address,
    device grants never match it, and the unattended ceiling refuses device
    and app operations before the engine — even with the envelope gate
    broken open and the owner holding the grant."""

    from server.agent import envelope as agent_envelope
    bob = await h.user("bob")
    agent, row = await _setup(h, bob)
    [first] = _occurrences(row, 1)
    monkeypatch.setattr(agent_envelope, "within_envelope", lambda *a, **k: True)
    monkeypatch.setattr(agent_envelope, "activation_within_envelope", lambda *a, **k: True)
    await h.grant(bob, "app.interact", resource_scope={"package_name": "com.example"})
    h.model.push(ask("app.interact", scope={"package_name": "com.example"}),
                 call("ui.app", "tap", args={"x": 1, "y": 1}, platform="android"), final("done"))
    await _tick(h, first + timedelta(minutes=1))
    assert h.ui.operations() == []
    [run] = await _runs(h, agent["agent_id"])
    assert run.kind == "unattended"


async def test_row_44_no_unattended_authority_survives_its_ground(h):
    """authorized → contained: revoking B's delegation stops B's next
    unattended run and the step of a live one (fresh checks, no cache)."""

    bob = await h.user("bob")
    agent, row = await _setup(h, bob)
    first, second = _occurrences(row, 2)
    h.model.push(final("one"))
    await _tick(h, first + timedelta(minutes=1))
    assert (await h.client.delete(f"/api/v1/agents/{agent['agent_id']}/delegation", headers=bob.auth)
            ).status_code == 200
    await _tick(h, second + timedelta(minutes=1))
    assert len(await _runs(h, agent["agent_id"])) == 1


async def test_row_45_in_process_code_can_forge_a_delegated_principal(h):
    """app-RCE → **REACHABLE** (accepted class, OD-A1 (a)): like `Principal`
    and `SuperuserGrant` (rows 3, 11), a `DelegatedPrincipal` is a value the
    process constructs; in-process code can build one naming A and have the
    engine decide as A. The engine trusts identity it is handed (03 §8); the
    runtime's freshness check is application code an attacker inside the
    process need not call."""

    alice = await h.user("alice")
    mine = await h.file(alice, None, Visibility.PRIVATE, "a.txt")
    await h.grant(alice, "file.read")
    forged = DelegatedPrincipal(user_id=alice.user_id, agent_id=uuid.uuid4(), delegation_id=uuid.uuid4(),
                                run_id=uuid.uuid4())
    outcome = await _decide(h, forged, operation=Operation.READ, resource_type=ResourceType.FILERESOURCE,
                            resource_ref=mine, required_capability="file.read", capability_operation="read_file")
    assert outcome.allowed   # REACHABLE — must stay so until isolation (b)/(c) exists
    # …but not through any legitimate path: the runtime refuses to run it.
    async with h.storage.session() as s:
        env = h.app.state.agent_tasks.environment(s, AuditLogger(s, request_id=uuid.uuid4()))
        assert not await env.security.principal_active(forged)
