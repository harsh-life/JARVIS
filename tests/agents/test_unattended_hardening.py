"""docs/29 §15, §26 — Phase 5 slice 5D: hardening of unattended runs.

* **Audit**: every step of a delegation's life and of each unattended run is
  recorded — ids and closed codes only — and never with a device or session
  that was not there.
* **Usage**: an unattended run's model and tool calls are metered and
  attributed to its run like any agent run's, as the owner's spend, with no
  device and no session.
* **The breaker** (18 §5): the operator's global stop and per-agent pause stop
  a live unattended run at its next step, and its delegation does not
  outlive the agent's pause.
* **Notices** are the owner's data: nobody else can read or delete them, and
  nothing in one is ever read as authority.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest

from server.security.events import AuditAction
from server.security.superuser import SUPERUSER_TOKEN_ENV
from server.storage.models import (
    AgentInboxItemRow,
    AgentRunRow,
    AgentRunTokenRow,
    AgentRunUsageRow,
    AuditEvent,
    StandingDelegationRow,
    UsageEvent,
)
from tests.agents.harness import API, TERMS, delegation_url
from tests.agents.test_unattended_runs import _occurrences, _runs, _setup, _tick
from tests.agents.harness import UNATTENDED_ON
from tests.evaluation.conftest import SU, TOKEN
from tests.runtime.conftest import ask, final

CONTROL = "/api/v1/admin/control"


@pytest.fixture
async def h(make_harness, monkeypatch):
    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, TOKEN)
    return await make_harness(config=UNATTENDED_ON)


async def _actions(h) -> set[str]:
    return {r.action for r in await h.rows(AuditEvent)}


# ── audit ─────────────────────────────────────────────────────────────────


async def test_a_delegations_whole_life_is_audited(h):
    alice = await h.user("alice")
    agent, row = await _setup(h, alice, terms={**TERMS, "expires_in_days": 2})
    first, second = _occurrences(row, 2)
    h.model.push(final("ok"))
    await _tick(h, first + timedelta(minutes=1))          # a run
    await _tick(h, second + timedelta(hours=3))           # a miss
    await h.client.delete(delegation_url(agent["agent_id"]), headers=alice.auth)   # a revoke
    actions = await _actions(h)
    for action in (AuditAction.AGENT_DELEGATION_GRANTED, AuditAction.AGENT_TOKEN_ISSUED,
                   AuditAction.AGENT_RUN_STARTED, AuditAction.AGENT_RUN_FINISHED,
                   AuditAction.AGENT_TOKEN_REVOKED, AuditAction.AGENT_INBOX_DELIVERED,
                   AuditAction.AGENT_UNATTENDED_SKIPPED, AuditAction.AGENT_DELEGATION_REVOKED):
        assert action.value in actions, action


async def test_expiry_and_invalidation_are_audited(h):
    alice = await h.user("alice")
    agent, row = await _setup(h, alice, terms={**TERMS, "expires_in_days": 1})
    await _tick(h, row.expires_at.replace(tzinfo=None).replace(tzinfo=__import__("datetime").timezone.utc)
                + timedelta(minutes=1))
    assert AuditAction.AGENT_DELEGATION_EXPIRED.value in await _actions(h)
    other, other_row = await _setup(h, alice)
    async with h.storage.session() as s:
        (await s.get(StandingDelegationRow, other_row.delegation_id)).spec_hash = "0" * 64
        await s.commit()
    [first] = _occurrences(other_row, 1)
    await _tick(h, first + timedelta(minutes=1))
    assert AuditAction.AGENT_DELEGATION_INVALIDATED.value in await _actions(h)


@pytest.mark.parametrize("change,action,reason", [
    ("pause", AuditAction.AGENT_DELEGATION_REVOKED, "paused"),
    ("operator_pause", AuditAction.AGENT_DELEGATION_REVOKED, "operator_paused"),
    ("delete", AuditAction.AGENT_DELEGATION_REVOKED, "deleted"),
    ("update", AuditAction.AGENT_DELEGATION_INVALIDATED, "spec_changed"),
    ("renew", AuditAction.AGENT_DELEGATION_REVOKED, "renewed"),
])
async def test_however_a_delegation_ends_it_is_audited(h, change, action, reason):
    from tests.agents.harness import HEADER, UNATTENDED_DRAFT, grant_delegation
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    agent_id = agent["agent_id"]
    if change == "pause":
        assert (await h.client.post(f"{API}/{agent_id}/pause", json={}, headers=alice.auth)).status_code == 200
    elif change == "operator_pause":
        assert (await h.client.post(f"{CONTROL}/agents/{agent_id}/pause", headers=SU,
                                    json={"reason": "abuse"})).status_code == 200
    elif change == "delete":
        first = await h.client.delete(f"{API}/{agent_id}", headers=alice.auth)
        token = first.json()["error"]["details"]["confirmation_token"]
        assert (await h.client.delete(f"{API}/{agent_id}", headers={**alice.auth, HEADER: token})).status_code == 204
    elif change == "update":
        compiled = await h.client.post(f"{API}/compile", params={"agent_id": agent_id}, headers=alice.auth,
                                       json={**UNATTENDED_DRAFT, "purpose": "Summarize critical advisories only."})
        compile_id = compiled.json()["compile_id"]
        first = await h.client.patch(f"{API}/{agent_id}", json={"compile_id": compile_id}, headers=alice.auth)
        token = first.json()["error"]["details"]["confirmation_token"]
        assert (await h.client.patch(f"{API}/{agent_id}", json={"compile_id": compile_id},
                                     headers={**alice.auth, HEADER: token})).status_code == 200
    else:
        await grant_delegation(h, alice, agent_id)
    rows = [r for r in await h.rows(AuditEvent) if r.action == action.value]
    assert any(f"agentdelegation:{row.delegation_id}:{reason}" == r.resource for r in rows), \
        [r.resource for r in rows]
    assert all(r.user_id == alice.user_id for r in rows)


async def test_an_unattended_runs_audit_names_no_device_and_no_session(h):
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    [first] = _occurrences(row, 1)
    h.model.push(ask("net.request"), final("ok"))
    await _tick(h, first + timedelta(minutes=1))
    [run] = await _runs(h, agent["agent_id"])
    task_rows = [r for r in await h.rows(AuditEvent) if str(run.task_id) in (r.resource or "")]
    assert task_rows and all(r.device_id is None and r.session_id is None for r in task_rows)
    # Nor is the device that granted the delegation ever named for it.
    assert all(r.device_id != alice.device_id for r in task_rows)


# ── usage ─────────────────────────────────────────────────────────────────


async def test_an_unattended_runs_usage_is_its_owners_and_attributed_to_its_run(h):
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    [first] = _occurrences(row, 1)
    h.model.push(final("ok"))
    await _tick(h, first + timedelta(minutes=1))
    [run] = await _runs(h, agent["agent_id"])
    attributed = await h.rows(AgentRunUsageRow, AgentRunUsageRow.run_id == run.run_id)
    assert attributed
    usage = await h.rows(UsageEvent, UsageEvent.usage_id.in_([a.usage_id for a in attributed]))
    assert usage and all((u.user_id, u.device_id, u.session_id) == (alice.user_id, None, None) for u in usage)


# ── the breaker and the operator ──────────────────────────────────────────


async def test_the_global_stop_stops_a_live_unattended_run(h):
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    [first] = _occurrences(row, 1)

    sent: list[asyncio.Task] = []

    async def stop(messages):
        # The operator's request is its own: it is not cancelled with the
        # model call it trips (that is the point of the trip).
        sent.append(asyncio.ensure_future(
            h.client.post(f"{CONTROL}/global-stop", headers=SU, json={"reason": "incident"})))
        await asyncio.shield(sent[0])
        return ask("net.request")

    h.model.push(stop, final("never reached"))
    await _tick(h, first + timedelta(minutes=1))
    assert (await sent[0]).status_code == 200
    [run] = await _runs(h, agent["agent_id"])
    assert (run.status, run.failure_code) == ("failed", "emergency_stop")
    assert "never reached" not in h.model.all_text()
    assert all(t.revoked_at is not None for t in await h.rows(AgentRunTokenRow, AgentRunTokenRow.run_id == run.run_id))


async def test_the_operators_agent_pause_stops_the_run_and_ends_the_delegation(h):
    alice = await h.user("alice")
    agent, row = await _setup(h, alice)
    [first] = _occurrences(row, 1)

    sent: list[asyncio.Task] = []

    async def pause(messages):
        sent.append(asyncio.ensure_future(
            h.client.post(f"{CONTROL}/agents/{agent['agent_id']}/pause", headers=SU, json={"reason": "abuse"})))
        await asyncio.shield(sent[0])
        return ask("net.request")

    h.model.push(pause, final("never reached"))
    await _tick(h, first + timedelta(minutes=1))
    assert (await sent[0]).status_code == 200
    [run] = await _runs(h, agent["agent_id"])
    assert run.status in ("failed", "cancelled") and "never reached" not in h.model.all_text()
    [after] = await h.rows(StandingDelegationRow, StandingDelegationRow.delegation_id == row.delegation_id)
    assert (after.status, after.status_reason) == ("revoked", "operator_paused")


# ── notices ───────────────────────────────────────────────────────────────


async def test_a_notice_is_its_owners_alone(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    agent, row = await _setup(h, alice)
    [first] = _occurrences(row, 1)
    await _tick(h, first + timedelta(hours=3))   # missed: a notice
    [notice] = await h.rows(AgentInboxItemRow, AgentInboxItemRow.kind == "notice")
    assert (notice.owner_user_id, notice.notice, notice.body, notice.run_id) == (
        alice.user_id, "run_missed", "", None)
    assert (await h.client.post(f"{API}/inbox/{notice.item_id}/read", json={}, headers=bob.auth)).status_code == 404
    assert (await h.client.delete(f"{API}/inbox/{notice.item_id}", headers=bob.auth)).status_code == 404
    mine = (await h.client.get(f"{API}/inbox", headers=alice.auth)).json()["items"]
    assert [(i["kind"], i["notice"]) for i in mine] == [("notice", "run_missed")]
    assert (await h.client.delete(f"{API}/inbox/{notice.item_id}", headers=alice.auth)).status_code == 204


async def test_nothing_in_the_api_writes_a_notice_or_a_delegation_for_an_agent(h):
    # The only writers are the owner's grant/revoke and the trigger loop;
    # there is no endpoint to post a notice, run unattended or extend one.
    paths = h.app.openapi()["paths"]
    assert {p for p in paths if "delegation" in p} == {"/api/v1/agents/{agent_id}/delegation"}
    assert set(paths["/api/v1/agents/{agent_id}/delegation"]) == {"get", "post", "delete"}
    assert not any("notice" in p or "unattended" in p or "trigger" in p for p in paths)
