"""docs/29 §15.5–§15.7 — Phase 5 slice 5C: unattended runs.

The trigger loop is the Agent Factory's own (`app.state.agent_triggers`), not
the scheduler's. Each pass: for every active delegation, re-validate it
fresh; claim the due occurrence (compare-and-set, so two passes never run it
twice); coalesce missed ones; skip — with a notice to the owner — when the
day's runs, the budget or the global latch say no; otherwise start the run as
the delegation's `DelegatedPrincipal` through the ordinary runtime: Agent
Gateway, envelope gate, unattended ceiling, the one engine, execution. An
unattended run never pauses for confirmation and never reaches a device.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from server.agent import envelope as agent_envelope
from server.scheduler.schedule import parse_schedule
from server.storage.models import (
    AgentInboxItemRow,
    AgentRunRow,
    AgentRunTokenRow,
    AgentTask,
    ConfirmationToken,
    StandingDelegationRow,
)
from tests.agents.harness import (
    API,
    TERMS,
    UNATTENDED_DRAFT,
    UNATTENDED_ON,
    create_agent,
    delegation_url,
    grant_delegation,
)
from tests.evaluation.conftest import SU, TOKEN
from tests.runtime.conftest import ask, call, final
from server.security.superuser import SUPERUSER_TOKEN_ENV

CONTROL = "/api/v1/admin/control"


@pytest.fixture
async def h(make_harness, monkeypatch):
    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, TOKEN)
    return await make_harness(config=UNATTENDED_ON)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _setup(h, actor, *, draft: dict | None = None, terms: dict | None = None):
    agent = await create_agent(h, actor, draft=draft or UNATTENDED_DRAFT)
    await grant_delegation(h, actor, agent["agent_id"], terms)
    [row] = await h.rows(StandingDelegationRow, StandingDelegationRow.agent_id == uuid.UUID(agent["agent_id"]),
                         StandingDelegationRow.status == "active")
    return agent, row


def _occurrences(row: StandingDelegationRow, count: int) -> list[datetime]:
    schedule = parse_schedule(f"CRON_TZ={row.timezone} {row.allowed_trigger_cron}")
    out, cursor = [], _utc(row.created_at)
    for _ in range(count):
        cursor = schedule.next_after(cursor)
        out.append(cursor)
    return out


async def _tick(h, now: datetime):
    return await h.app.state.agent_triggers.tick(now=now)


async def _notices(h, agent_id: str) -> list[str]:
    rows = await h.rows(AgentInboxItemRow, AgentInboxItemRow.agent_id == uuid.UUID(agent_id),
                        AgentInboxItemRow.kind == "notice")
    return sorted(r.notice for r in rows)


async def _runs(h, agent_id: str) -> list[AgentRunRow]:
    return await h.rows(AgentRunRow, AgentRunRow.agent_id == uuid.UUID(agent_id))


async def _unattended_confirmations(h, agent_id: str) -> list[ConfirmationToken]:
    """Confirmation tokens issued inside the agent's unattended runs' tasks
    (the owner's own HTTP grant/create confirmations are not these)."""

    tasks = [str(r.task_id) for r in await _runs(h, agent_id) if r.task_id is not None]
    return await h.rows(ConfirmationToken, ConfirmationToken.task_id.in_(tasks)) if tasks else []


# ── the run ───────────────────────────────────────────────────────────────


async def test_nothing_runs_before_the_occurrence(h):
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    [first] = _occurrences(row, 1)
    await _tick(h, first - timedelta(minutes=1))
    assert await _runs(h, agent["agent_id"]) == []


async def test_a_due_occurrence_runs_once_as_the_delegated_principal(h):
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    [first] = _occurrences(row, 1)
    h.model.push(final("Two critical advisories."))
    await _tick(h, first + timedelta(minutes=1))
    await _tick(h, first + timedelta(minutes=2))          # the same occurrence again: claimed already
    [run] = await _runs(h, agent["agent_id"])
    assert (run.kind, run.status, run.delegation_id, _utc(run.occurrence_at)) == (
        "unattended", "completed", row.delegation_id, first)
    # The task is the delegation's: no device, no session.
    [task] = await h.rows(AgentTask, AgentTask.task_id == run.task_id)
    assert (task.user_id, task.device_id, task.session_id, task.delegation_id) == (
        alice.user_id, None, None, row.delegation_id)
    # Its result went to the owner's inbox, as data; its tokens ended with it.
    [item] = await h.rows(AgentInboxItemRow, AgentInboxItemRow.run_id == run.run_id)
    assert (item.kind, item.status, item.body) == ("result", "completed", "Two critical advisories.")
    assert all(t.revoked_at is not None for t in await h.rows(AgentRunTokenRow, AgentRunTokenRow.run_id == run.run_id))
    # And the owner sees it like any of their runs.
    listed = await h.client.get(f"{API}/{agent['agent_id']}/runs", headers=alice.auth)
    assert [r["kind"] for r in listed.json()["items"]] == ["unattended"]


async def test_two_concurrent_passes_run_an_occurrence_once(h):
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    [first] = _occurrences(row, 1)
    h.model.push(final("one"), final("two"))
    await asyncio.gather(_tick(h, first + timedelta(minutes=1)), _tick(h, first + timedelta(minutes=1)))
    assert len(await _runs(h, agent["agent_id"])) == 1


# ── misfires: once, coalesced, never a storm ──────────────────────────────


async def test_a_long_outage_runs_once_and_tells_the_owner_what_was_missed(h):
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    occurrences = _occurrences(row, 4)
    h.model.push(final("caught up"))
    await _tick(h, occurrences[-1] + timedelta(minutes=2))
    [run] = await _runs(h, agent["agent_id"])
    assert _utc(run.occurrence_at) == occurrences[-1]
    assert await _notices(h, agent["agent_id"]) == ["misfire_coalesced"]


async def test_past_the_grace_nothing_runs_and_the_miss_is_recorded(h):
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    [first] = _occurrences(row, 1)
    await _tick(h, first + timedelta(hours=2))
    assert await _runs(h, agent["agent_id"]) == []
    assert await _notices(h, agent["agent_id"]) == ["run_missed"]
    [after] = await h.rows(StandingDelegationRow, StandingDelegationRow.delegation_id == row.delegation_id)
    assert _utc(after.last_occurrence_at) == first


# ── limits: day, budget, latch, expiry ────────────────────────────────────


async def test_the_days_run_limit_skips_with_a_notice(h):
    alice = await h.user("alice")
    hourly = {**UNATTENDED_DRAFT, "trigger_request": {"kind": "unattended", "cron": "0 * * * *", "timezone": "UTC"}}
    agent, row = await _setup(h, alice, draft=hourly, terms={**TERMS, "max_runs_per_day": 1})
    first, second = _occurrences(row, 2)
    if first.date() != second.date():
        # Keep both in one UTC day: the occurrence before midnight counts as
        # already claimed, so it is neither run nor coalesced into a notice.
        skipped, first, second = _occurrences(row, 3)
        async with h.storage.session() as s:
            await s.execute(update(StandingDelegationRow)
                            .where(StandingDelegationRow.delegation_id == row.delegation_id)
                            .values(last_occurrence_at=skipped))
            await s.commit()
    h.model.push(final("one"))
    await _tick(h, first + timedelta(minutes=1))
    await _tick(h, second + timedelta(minutes=1))
    assert len(await _runs(h, agent["agent_id"])) == 1
    assert await _notices(h, agent["agent_id"]) == ["run_limit_reached"]


async def test_a_spent_month_skips_with_a_notice(h):
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    [first] = _occurrences(row, 1)
    async with h.storage.session() as s:
        s.add(AgentRunRow(run_id=uuid.uuid4(), agent_id=row.agent_id, owner_user_id=alice.user_id, version=1,
                          spec_hash=row.spec_hash, kind="on_demand", status="completed",
                          cost_total=row.budget_per_month, started_at=first - timedelta(hours=1),
                          finished_at=first - timedelta(hours=1)))
        await s.commit()
    await _tick(h, first + timedelta(minutes=1))
    assert [r.kind for r in await _runs(h, agent["agent_id"])] == ["on_demand"]
    assert await _notices(h, agent["agent_id"]) == ["budget_exhausted"]


async def test_the_global_latch_stops_unattended_runs(h):
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    [first] = _occurrences(row, 1)
    latched = await h.client.post(f"{CONTROL}/global-stop", headers=SU, json={"reason": "incident"})
    assert latched.status_code == 200, latched.text
    await _tick(h, first + timedelta(minutes=1))
    assert await _runs(h, agent["agent_id"]) == [] and await h.rows(AgentTask) == []
    assert await _notices(h, agent["agent_id"]) == ["breaker_stopped"]


async def test_an_expired_delegation_runs_nothing_and_says_so(h):
    alice = await h.user("alice")
    agent, row = await _setup(h, alice, terms={**TERMS, "expires_in_days": 1})
    await _tick(h, _utc(row.expires_at) - timedelta(hours=1))
    assert "delegation_expiring" in await _notices(h, agent["agent_id"])
    await _tick(h, _utc(row.expires_at) - timedelta(minutes=30))
    assert (await _notices(h, agent["agent_id"])).count("delegation_expiring") == 1   # once
    await _tick(h, _utc(row.expires_at) + timedelta(minutes=1))
    [after] = await h.rows(StandingDelegationRow, StandingDelegationRow.delegation_id == row.delegation_id)
    assert after.status == "expired"
    assert "delegation_expired" in await _notices(h, agent["agent_id"])
    assert all(r.status != "running" for r in await _runs(h, agent["agent_id"]))


# ── revocation and freshness ──────────────────────────────────────────────


async def test_a_revoked_delegation_runs_nothing(h):
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    [first] = _occurrences(row, 1)
    assert (await h.client.delete(delegation_url(agent["agent_id"]), headers=alice.auth)).status_code == 200
    await _tick(h, first + timedelta(minutes=1))
    assert await _runs(h, agent["agent_id"]) == []


async def test_a_deleted_or_paused_agent_runs_nothing(h):
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    [first] = _occurrences(row, 1)
    assert (await h.client.post(f"{API}/{agent['agent_id']}/pause", json={}, headers=alice.auth)).status_code == 200
    await _tick(h, first + timedelta(minutes=1))
    assert await _runs(h, agent["agent_id"]) == []


async def test_a_delegation_whose_spec_moved_on_is_invalidated_at_the_next_pass(h):
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    [first] = _occurrences(row, 1)
    async with h.storage.session() as s:
        stored = await s.get(StandingDelegationRow, row.delegation_id)
        stored.envelope_hash = "0" * 64   # as if granted for a different envelope
        await s.commit()
    await _tick(h, first + timedelta(minutes=1))
    assert await _runs(h, agent["agent_id"]) == []
    [after] = await h.rows(StandingDelegationRow, StandingDelegationRow.delegation_id == row.delegation_id)
    assert (after.status, after.status_reason) == ("invalidated", "envelope_changed")
    assert await _notices(h, agent["agent_id"]) == ["delegation_invalidated"]


async def test_a_suspended_owner_runs_nothing(h):
    from server.storage.models import User
    from shared.schemas.enums import UserStatus
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    [first] = _occurrences(row, 1)
    async with h.storage.session() as s:
        (await s.get(User, alice.user_id)).status = UserStatus.SUSPENDED
        await s.commit()
    await _tick(h, first + timedelta(minutes=1))
    assert await _runs(h, agent["agent_id"]) == []
    [after] = await h.rows(StandingDelegationRow, StandingDelegationRow.delegation_id == row.delegation_id)
    assert after.status == "invalidated"


# ── the unattended ceiling at run time ────────────────────────────────────


async def test_an_unattended_run_never_asks_for_confirmation(h):
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    [first] = _occurrences(row, 1)
    # No standing grant for net.request: a present owner would be asked to
    # activate it; the unattended run is refused and carries on.
    h.model.push(ask("net.request"), final("could not fetch"))
    await _tick(h, first + timedelta(minutes=1))
    [run] = await _runs(h, agent["agent_id"])
    assert run.status == "completed"
    assert await _unattended_confirmations(h, agent["agent_id"]) == []
    assert "not granted" in h.model.all_text() or "never asks" in h.model.all_text()


async def test_the_owners_standing_grant_is_used_and_its_loss_is_felt_at_once(h):
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    first, second = _occurrences(row, 2)
    grant_id = await h.grant(alice, "net.request")
    h.model.push(ask("net.request"), final("fetched"))
    await _tick(h, first + timedelta(minutes=1))
    assert "existing grant" in h.model.all_text()
    await h.revoke(alice, grant_id)
    h.model.push(ask("net.request"), final("could not"))
    await _tick(h, second + timedelta(minutes=1))
    runs = sorted(await _runs(h, agent["agent_id"]), key=lambda r: r.started_at)
    assert [r.status for r in runs] == ["completed", "completed"]
    assert await _unattended_confirmations(h, agent["agent_id"]) == []


@pytest.mark.parametrize("capability,scope,proposal", [
    ("app.interact", {"package_name": "com.example"}, call("ui.app", "tap", args={"x": 1, "y": 1},
                                                          platform="android")),
    ("file.write", None, call("files.write", "write_file", args={"path": "x", "content": "y"})),
    ("agent.define", None, call("agent.define", "compile", args={})),
    ("system.restricted", None, call("files.read", "read_file", args={"path": "x"})),
])
async def test_the_unattended_ceiling_holds_even_if_the_envelope_gate_failed_open(h, monkeypatch, capability,
                                                                                  scope, proposal):
    # Defence in depth: with the envelope gate broken open, a device UI call,
    # agent creation and break-glass are still refused by the unattended
    # ceiling — before the engine, never as a confirmation, never on a device.
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    [first] = _occurrences(row, 1)
    monkeypatch.setattr(agent_envelope, "within_envelope", lambda *a, **k: True)
    monkeypatch.setattr(agent_envelope, "activation_within_envelope", lambda *a, **k: True)
    if capability in ("app.interact", "file.write"):
        await h.grant(alice, capability, resource_scope=scope)
    h.model.push(ask(capability, scope=scope), proposal, final("tried"))
    await _tick(h, first + timedelta(minutes=1))
    assert h.ui.operations() == [] and h.writes.calls == []
    # Refused at activation already — never "active for this task".
    assert f"{capability}: active for this task" not in h.model.all_text()
    assert await _unattended_confirmations(h, agent["agent_id"]) == []
    [run] = await _runs(h, agent["agent_id"])
    assert run.status in ("completed", "failed")
    [task] = await h.rows(AgentTask, AgentTask.task_id == run.task_id)
    assert task.status != "awaiting_confirmation"


async def test_an_operation_above_the_ceiling_is_refused_by_the_run_itself(h, monkeypatch):
    # Every shipped unattended template is observe-mode, so its mode ceiling
    # already refuses writes. With that and the envelope gate both broken
    # open, a consequential operation on a capability the owner granted is
    # still refused by the run's own unattended ceiling — before the engine.
    from server.agent import modes
    from shared.schemas.enums import Visibility
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    [first] = _occurrences(row, 1)
    monkeypatch.setattr(agent_envelope, "within_envelope", lambda *a, **k: True)
    monkeypatch.setattr(agent_envelope, "activation_within_envelope", lambda *a, **k: True)
    monkeypatch.setattr(modes, "within_ceiling", lambda *a, **k: True)
    monkeypatch.setattr(modes, "capability_usable", lambda *a, **k: True)
    await h.grant(alice, "file.write")
    f = await h.file(alice, None, Visibility.PRIVATE, "a.txt")
    h.model.push(ask("file.write"), call("files.write", "delete_file", ref=f), final("tried"))
    await _tick(h, first + timedelta(minutes=1))
    assert "file.write: outside what an unattended run may ever do" in h.model.all_text()
    assert h.writes.calls == []
    assert await _unattended_confirmations(h, agent["agent_id"]) == []


async def test_an_unattended_run_spends_at_most_its_delegations_month(h):
    from server.agent.agent_run import AgentRunBinding, RunTokens
    from server.composition.agents import AgentRunCoordinator
    from server.security.audit import AuditLogger
    import dataclasses
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)   # spec: 1.0 a month; delegation: 0.2
    [first] = _occurrences(row, 1)
    factory = h.app.state.agent_factory.factory
    async with h.storage.session() as s:
        stored = await s.get(StandingDelegationRow, row.delegation_id)
        _, spec = await factory.service.load(s, stored.agent_id)
        run = await factory.service.create_run(s, spec=spec, run_id=uuid.uuid4(), delegation=stored,
                                               occurrence_at=first)
        await s.commit()
    binding = AgentRunBinding.from_spec(spec, run_id=run.run_id, model_ref="agent.primary", budget_per_run=0.01,
                                        delegation_id=row.delegation_id)
    async with h.storage.session() as s:
        coordinator = AgentRunCoordinator(factory, s, AuditLogger(s, request_id=uuid.uuid4()), h.core)
        assert spec.budget.per_month > 0.5 > row.budget_per_month
        assert await coordinator.agent_budget(binding, projected_cost=0.5) == "agent_budget_exhausted"
        assert await coordinator.agent_budget(binding, projected_cost=0.1) is None


# ── mid-run: no authority survives its loss ───────────────────────────────


@pytest.mark.parametrize("loss", ["delegation_revoked", "owner_suspended", "agent_paused"])
async def test_losing_any_ground_mid_run_stops_the_very_next_step(h, loss):
    from server.storage.models import AgentDefinitionRow, User
    from shared.schemas.enums import UserStatus
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    [first] = _occurrences(row, 1)

    async def lose(messages):
        async with h.storage.session() as s:
            if loss == "delegation_revoked":
                (await s.get(StandingDelegationRow, row.delegation_id)).status = "revoked"
            elif loss == "owner_suspended":
                (await s.get(User, alice.user_id)).status = UserStatus.SUSPENDED
            else:
                (await s.get(AgentDefinitionRow, uuid.UUID(agent["agent_id"]))).status = "paused"
            await s.commit()
        return ask("net.request")

    h.model.push(lose, final("should never be reached"))
    await _tick(h, first + timedelta(minutes=1))
    [run] = await _runs(h, agent["agent_id"])
    assert run.status == "failed" and run.failure_code in ("agent_unavailable", "principal_revoked")
    assert "should never be reached" not in h.model.all_text()
    assert all(t.revoked_at is not None for t in await h.rows(AgentRunTokenRow, AgentRunTokenRow.run_id == run.run_id))


async def test_the_runs_every_step_rechecks_its_delegation(h):
    # The coordinator's own check (the security port's principal check is a
    # second, independent one): a run whose delegation is revoked, expired or
    # changed cannot take its next step.
    from server.agent.agent_run import AgentRunBinding, RunTokens
    from server.composition.agents import AgentRunCoordinator
    from server.security.audit import AuditLogger
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    [first] = _occurrences(row, 1)
    factory = h.app.state.agent_factory.factory
    async with h.storage.session() as s:
        stored = await s.get(StandingDelegationRow, row.delegation_id)
        _, spec = await factory.service.load(s, stored.agent_id)
        run = await factory.service.create_run(s, spec=spec, run_id=uuid.uuid4(), delegation=stored,
                                               occurrence_at=first)
        issued = await factory.gateway.issue(s, run, deadline=factory.run_deadline(spec))
        await s.commit()
    binding = AgentRunBinding.from_spec(spec, run_id=run.run_id, model_ref="agent.primary", budget_per_run=0.01,
                                        delegation_id=row.delegation_id)
    import dataclasses
    binding = dataclasses.replace(binding, run_tokens=RunTokens(model=issued.model, tool=issued.tool))

    async def check():
        async with h.storage.session() as s:
            return await AgentRunCoordinator(factory, s, AuditLogger(s, request_id=uuid.uuid4()), h.core).check(
                binding)

    assert await check() is None
    async with h.storage.session() as s:
        (await s.get(StandingDelegationRow, row.delegation_id)).status = "revoked"
        await s.commit()
    assert (await check()).value == "agent_unavailable"


async def test_a_delegated_principal_runs_only_its_own_unattended_run(h):
    from shared.schemas.authorization import DelegatedPrincipal
    from server.security.audit import AuditLogger
    alice = await h.user("alice")
    principal = DelegatedPrincipal(user_id=alice.user_id, agent_id=uuid.uuid4(), delegation_id=uuid.uuid4(),
                                   run_id=uuid.uuid4())
    async with h.storage.session() as s:
        with pytest.raises(RuntimeError):
            await h.app.state.agent_tasks.submit(s, principal=principal, user_input="do anything",
                                                 audit=AuditLogger(s, request_id=uuid.uuid4()))
    assert await h.rows(AgentTask) == []


# ── restarts ──────────────────────────────────────────────────────────────


async def test_an_interrupted_unattended_run_is_closed_when_the_loop_starts(h):
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    [first] = _occurrences(row, 1)
    service = h.app.state.agent_factory.factory.service
    async with h.storage.session() as s:
        stored = await s.get(StandingDelegationRow, row.delegation_id)
        loaded = await service.load(s, stored.agent_id)
        assert await service.claim_occurrence(s, stored.delegation_id, first)
        run = await service.create_run(s, spec=loaded[1], run_id=uuid.uuid4(), delegation=stored,
                                       occurrence_at=first)
        await s.commit()
    # The process died before the run's task existed. At the next start:
    await h.app.state.agent_triggers.reconcile()
    [closed] = await h.rows(AgentRunRow, AgentRunRow.run_id == run.run_id)
    assert closed.status == "failed" and closed.failure_code == "interrupted" and closed.finished_at is not None
    # Nothing is replayed: the claimed occurrence never runs again.
    await _tick(h, first + timedelta(minutes=2))
    assert len(await _runs(h, agent["agent_id"])) == 1


# ── the scheduler stays out of it ─────────────────────────────────────────


def test_the_scheduler_has_no_path_to_an_unattended_run():
    import pathlib
    for path in pathlib.Path("server/scheduler").glob("*.py"):
        text = path.read_text()
        assert "server.agents" not in text and "agent_triggers" not in text and "DelegatedPrincipal" not in text


# ── isolation ─────────────────────────────────────────────────────────────


async def test_an_owners_unattended_results_and_notices_are_theirs_alone(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    agent, row = await _setup(h, alice)
    occurrences = _occurrences(row, 3)
    h.model.push(final("for alice"))
    await _tick(h, occurrences[-1] + timedelta(minutes=1))
    inbox = await h.client.get(f"{API}/inbox", headers=bob.auth)
    assert inbox.status_code == 200 and inbox.json()["items"] == []
    mine = await h.client.get(f"{API}/inbox", headers=alice.auth)
    kinds = sorted(i["kind"] for i in mine.json()["items"])
    assert kinds == ["notice", "result"]
    assert (await h.client.get(f"{API}/{agent['agent_id']}/runs", headers=bob.auth)).status_code == 404
