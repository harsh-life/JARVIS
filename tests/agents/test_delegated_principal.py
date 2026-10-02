"""docs/29 §15.2 — Phase 5 slice 5B: the DelegatedPrincipal (`03` §8 / `04`
§1 as amended by OD-AF-2, DECISION_REGISTER §2K).

An unattended run is not a present user. It is `(owner, agent, delegation,
run, graph?)` and **structurally** has no device and no session:

* reading a device or session off it fails, and a device- or session-scoped
  grant never matches it — the owner's `user`/`graph` grants do;
* the one engine decides it on D1–D5 like any principal, and anything that
  would need the owner's confirmation is a refusal, never a pause (OD-AF-4);
* it is fresh only while the owner is active and still in the graph, the
  delegation is active, unexpired and exactly the agent's current spec, and
  its run is open — re-read every time, never cached;
* claiming a schedule occurrence is a compare-and-set on the delegation, so
  two trigger workers never run one occurrence twice.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from server.graph.authorization import AccessRequest
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from sqlalchemy import select

from server.storage.models import AgentRunRow, AuditEvent, CapabilityGrant, StandingDelegationRow, User
from server.storage.models import Session as SessionRow
from shared.schemas.authorization import (
    CapabilityCheckContext,
    DelegatedPrincipal,
    Operation,
    Principal,
    ResourceType,
    device_of,
    is_delegated,
    session_of,
)
from shared.schemas.enums import CapabilityScopeType, PermissionDecisionValue, UserStatus
from tests.agents.harness import UNATTENDED_DRAFT, UNATTENDED_ON, create_agent, grant_delegation

OCCURRENCE = datetime(2026, 10, 3, 1, 30, tzinfo=timezone.utc)


@pytest.fixture
async def h(make_harness):
    return await make_harness(config=UNATTENDED_ON)


def _principal(**overrides) -> DelegatedPrincipal:
    base = dict(user_id=uuid.uuid4(), agent_id=uuid.uuid4(), delegation_id=uuid.uuid4(), run_id=uuid.uuid4())
    base.update(overrides)
    return DelegatedPrincipal(**base)


# ── the type ──────────────────────────────────────────────────────────────


def test_a_delegated_principal_has_no_device_and_no_session():
    principal = _principal()
    for name in ("device_id", "session_id"):
        with pytest.raises(AttributeError):
            getattr(principal, name)
    assert (device_of(principal), session_of(principal), is_delegated(principal)) == (None, None, True)
    assert principal.active_graph_id is None


@pytest.mark.parametrize("extra", [{"device_id": uuid.uuid4()}, {"session_id": uuid.uuid4()},
                                   {"capabilities": ["file.write"]}])
def test_nothing_can_be_smuggled_into_one(extra):
    with pytest.raises(ValidationError):
        _principal(**extra)


def test_it_is_immutable_and_distinct_from_a_present_user():
    principal = _principal()
    with pytest.raises(ValidationError):
        principal.user_id = uuid.uuid4()  # type: ignore[misc]
    present = Principal(user_id=uuid.uuid4(), device_id=uuid.uuid4(), session_id=uuid.uuid4())
    assert not is_delegated(present) and device_of(present) == present.device_id


# ── helpers over the production composition ───────────────────────────────


async def _delegated(h, actor, *, agent: dict | None = None) -> tuple[dict, DelegatedPrincipal]:
    """An unattended agent with a granted delegation and one open run (as the
    trigger loop opens it), and the principal that run carries."""

    agent = agent or await create_agent(h, actor, draft=UNATTENDED_DRAFT)
    await grant_delegation(h, actor, agent["agent_id"])
    factory = h.app.state.agent_factory.factory
    async with h.storage.session() as s:
        [delegation] = (await s.execute(StandingDelegationRow.__table__.select().where(
            StandingDelegationRow.agent_id == uuid.UUID(agent["agent_id"]),
            StandingDelegationRow.status == "active"))).all()
        row = await s.get(StandingDelegationRow, delegation.delegation_id)
        loaded = await factory.service.load(s, row.agent_id)
        assert loaded is not None and loaded[1] is not None
        assert await factory.service.claim_occurrence(s, row.delegation_id, OCCURRENCE)
        run = await factory.service.create_run(s, spec=loaded[1], run_id=uuid.uuid4(), delegation=row,
                                               occurrence_at=OCCURRENCE)
        await s.commit()
    return agent, DelegatedPrincipal(user_id=actor.user_id, agent_id=row.agent_id,
                                     delegation_id=row.delegation_id, run_id=run.run_id, graph_id=row.graph_id)


async def _authorize(h, principal, **request):
    async with h.storage.session() as s:
        outcome = await h.core.engine.authorize(s, AccessRequest(principal=principal, **request),
                                                audit=AuditLogger(s, request_id=uuid.uuid4()))
        await s.commit()
    return outcome


async def _active(h, principal) -> bool:
    async with h.storage.session() as s:
        env = h.app.state.agent_tasks.environment(s, AuditLogger(s, request_id=uuid.uuid4()))
        return await env.security.principal_active(principal)


# ── the engine decides it like any principal, but never asks it to confirm ─


async def test_the_engine_decides_a_delegated_principal_on_its_owners_dimensions(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    agent, principal = await _delegated(h, alice)
    bobs = await create_agent(h, bob, draft=UNATTENDED_DRAFT)
    read = dict(operation=Operation.READ, resource_type=ResourceType.AGENTDEFINITION)
    assert (await _authorize(h, principal, resource_ref=agent["agent_id"], **read)).allowed
    theirs = await _authorize(h, principal, resource_ref=bobs["agent_id"], **read)
    assert not theirs.allowed and theirs.surface.value == "not_found"
    # The decision is audited with no device and no session — there are none.
    rows = [r for r in await h.rows(AuditEvent) if r.action == AuditAction.AUTHORIZATION_DECIDED.value
            and r.user_id == alice.user_id and r.device_id is None]
    assert rows and all(r.session_id is None for r in rows)


async def test_what_would_need_the_owners_confirmation_is_refused_never_paused(h):
    alice = await h.user("alice")
    agent, principal = await _delegated(h, alice)
    # Changing the agent is consequential: for a present owner it asks for a
    # confirmation; for an unattended run it is a refusal, with no token.
    outcome = await _authorize(h, principal, operation=Operation.WRITE, resource_type=ResourceType.AGENTDEFINITION,
                               resource_ref=agent["agent_id"], arguments={"action": "x"})
    assert outcome.decision is PermissionDecisionValue.DENY
    assert outcome.confirmation_required_for is None and outcome.reason == "unattended_never_confirms"


async def test_only_the_owners_user_and_graph_grants_match_it(h):
    alice = await h.user("alice")
    _, principal = await _delegated(h, alice)
    grants = h.core.capability_grants

    async def has() -> bool:
        async with h.storage.session() as s:
            return await grants.has_capability(s, capability="file.read",
                                               context=CapabilityCheckContext(principal=principal))

    async with h.storage.session() as s:
        session_id = (await s.execute(select(SessionRow.session_id).where(
            SessionRow.device_id == alice.device_id))).scalars().first()
        for scope_type, scope_id in ((CapabilityScopeType.DEVICE, alice.device_id),
                                     (CapabilityScopeType.SESSION, session_id)):
            await grants.grant(s, principal_id=scope_id, scope_type=scope_type, capability="file.read",
                               granted_by=alice.user_id)
        await s.commit()
    assert not await has()   # the owner's device and session grants are not the agent's
    await h.grant(alice, "file.read")
    assert await has()       # the owner's own (user-scoped) grant is


# ── freshness: re-read at every step, never cached ────────────────────────


async def test_a_fresh_delegated_principal_is_active(h):
    alice = await h.user("alice")
    _, principal = await _delegated(h, alice)
    assert await _active(h, principal)


@pytest.mark.parametrize("loss", ["revoked", "owner_suspended", "run_finished", "expired", "other_run",
                                  "other_agent", "spec_changed"])
async def test_it_stops_being_active_the_moment_anything_it_rests_on_goes(h, loss):
    alice = await h.user("alice")
    agent, principal = await _delegated(h, alice)
    async with h.storage.session() as s:
        delegation = await s.get(StandingDelegationRow, principal.delegation_id)
        if loss == "revoked":
            delegation.status, delegation.revoked_at = "revoked", datetime.now(timezone.utc)
        elif loss == "owner_suspended":
            (await s.get(User, alice.user_id)).status = UserStatus.SUSPENDED
        elif loss == "run_finished":
            (await s.get(AgentRunRow, principal.run_id)).finished_at = datetime.now(timezone.utc)
        elif loss == "expired":
            delegation.created_at = datetime.now(timezone.utc) - timedelta(days=31)
            delegation.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        elif loss == "spec_changed":
            delegation.spec_hash = "0" * 64
        await s.commit()
    if loss == "other_run":
        principal = principal.model_copy(update={"run_id": uuid.uuid4()})
    if loss == "other_agent":
        principal = principal.model_copy(update={"agent_id": uuid.uuid4()})
    assert not await _active(h, principal)


async def test_with_the_factory_off_no_delegated_principal_is_ever_active(make_harness):
    from tests.agents.harness import AGENTS_ON
    h = await make_harness(config={k: v for k, v in AGENTS_ON.items() if k != "agents"})
    assert not await _active(h, _principal())


# ── admission: one occurrence, one claim ──────────────────────────────────


async def test_an_occurrence_is_claimed_once_and_never_backwards(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=UNATTENDED_DRAFT)
    await grant_delegation(h, alice, agent["agent_id"])
    service = h.app.state.agent_factory.factory.service
    [row] = await h.rows(StandingDelegationRow)

    async def claim(at: datetime) -> bool:
        async with h.storage.session() as s:
            won = await service.claim_occurrence(s, row.delegation_id, at)
            await s.commit()
            return won

    later = OCCURRENCE + timedelta(days=1)
    results = await asyncio.gather(claim(OCCURRENCE), claim(OCCURRENCE), claim(OCCURRENCE))
    assert sorted(results) == [False, False, True]
    assert await claim(later) and not await claim(OCCURRENCE) and not await claim(later)


async def test_a_revoked_delegation_claims_nothing(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=UNATTENDED_DRAFT)
    await grant_delegation(h, alice, agent["agent_id"])
    await h.client.delete(f"/api/v1/agents/{agent['agent_id']}/delegation", headers=alice.auth)
    service = h.app.state.agent_factory.factory.service
    [row] = await h.rows(StandingDelegationRow)
    async with h.storage.session() as s:
        assert not await service.claim_occurrence(s, row.delegation_id, OCCURRENCE)


async def test_granting_a_delegation_grants_no_capability(h):
    alice = await h.user("alice")
    await _delegated(h, alice)
    # A delegation is a ceiling: granting one writes no capability grant.
    assert await h.rows(CapabilityGrant) == []
