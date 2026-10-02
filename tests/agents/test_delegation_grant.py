"""docs/29 §15.3–§15.4 — Phase 5 slice 5B: granting and losing a standing
delegation (OD-AF-2/3/5/7, DECISION_REGISTER §2K).

* Granting is the direction that gives authority: the owner's own HTTP call,
  decided by the engine on the agent (consequential → a confirmation bound to
  the exact terms), then a fresh step-up (device re-attestation). No task,
  model or agent can grant one (it is not a tool).
* The terms come from the stored spec; the owner only chooses tighter limits.
* Revoking is the safe direction: always allowed, never confirmed.
* Anything that changes the agent ends its delegation — pause (owner or
  operator), a new version, re-approval, deletion — and nothing gives it
  back except a new step-up grant.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from server.security.events import AuditAction
from server.security.superuser import SUPERUSER_TOKEN_ENV
from server.storage.models import AgentDefinitionRow, AgentInboxItemRow, AuditEvent, StandingDelegationRow
from tests.agents.harness import (
    AGENTS_ON,
    API,
    DRAFT,
    HEADER,
    TERMS,
    UNATTENDED_DRAFT,
    UNATTENDED_ON,
    create_agent,
    delegation_url,
    grant_delegation,
    request_delegation,
)
from tests.evaluation.conftest import SU, TOKEN


@pytest.fixture
async def h(make_harness, monkeypatch):
    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, TOKEN)
    return await make_harness(config=UNATTENDED_ON)


async def _delegations(h, agent_id: str) -> list[StandingDelegationRow]:
    return await h.rows(StandingDelegationRow, StandingDelegationRow.agent_id == uuid.UUID(agent_id))


async def _notices(h, agent_id: str) -> list[str]:
    rows = await h.rows(AgentInboxItemRow, AgentInboxItemRow.agent_id == uuid.UUID(agent_id),
                        AgentInboxItemRow.kind == "notice")
    return [r.notice for r in rows]


# ── granting ──────────────────────────────────────────────────────────────


async def test_a_grant_needs_the_owners_confirmation_and_a_fresh_step_up(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=UNATTENDED_DRAFT)
    first = await request_delegation(h, alice, agent["agent_id"])
    assert first.status_code == 403, first.text
    details = first.json()["error"]["details"]
    assert details["action"] == "grant_standing_delegation" and details["card"]
    assert await _delegations(h, agent["agent_id"]) == []

    # The confirmed retry without a fresh re-attestation: refused, and the
    # token is not spent — the owner steps up and retries with it.
    stale = await request_delegation(h, alice, agent["agent_id"], token=details["confirmation_token"])
    assert stale.status_code == 401 and stale.json()["error"]["details"]["step_up_required"], stale.text
    assert await _delegations(h, agent["agent_id"]) == []

    await h.step_up(alice)
    granted = await request_delegation(h, alice, agent["agent_id"], token=details["confirmation_token"])
    assert granted.status_code == 201, granted.text
    view = granted.json()
    assert view["status"] == "active" and view["schedule"] == "0 7 * * *" and view["timezone"] == "Asia/Kolkata"

    [row] = await _delegations(h, agent["agent_id"])
    assert row.created_with_step_up and row.created_by_device_id == alice.device_id
    assert (row.owner_user_id, row.max_runs_per_day, row.budget_per_run, row.budget_per_month) == (
        alice.user_id, 2, 0.01, 0.2)
    [definition] = await h.rows(AgentDefinitionRow, AgentDefinitionRow.agent_id == uuid.UUID(agent["agent_id"]))
    assert row.spec_version == definition.current_version
    expires = row.expires_at.replace(tzinfo=timezone.utc) if row.expires_at.tzinfo is None else row.expires_at
    assert timedelta(days=29, hours=23) < expires - datetime.now(timezone.utc) <= timedelta(days=30)  # OD-AF-5
    actions = {r.action for r in await h.rows(AuditEvent)}
    assert AuditAction.AGENT_DELEGATION_GRANTED.value in actions


async def test_the_confirmation_is_bound_to_the_exact_terms(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=UNATTENDED_DRAFT)
    first = await request_delegation(h, alice, agent["agent_id"])
    token = first.json()["error"]["details"]["confirmation_token"]
    await h.step_up(alice)
    wider = await request_delegation(h, alice, agent["agent_id"], {**TERMS, "max_runs_per_day": 24}, token)
    assert wider.status_code == 403 and wider.json()["error"]["code"] == "confirmation_required"
    assert await _delegations(h, agent["agent_id"]) == []


@pytest.mark.parametrize("terms,status,reason", [
    ({**TERMS, "budget_per_run": 0.5}, 409, "budget_above_spec"),
    ({**TERMS, "budget_per_month": 5.0}, 409, "budget_above_spec"),
    ({**TERMS, "expires_in_days": 31}, 409, "expiry_too_long"),
    ({**TERMS, "budget_per_run": 0.0}, 422, None),
    ({**TERMS, "max_runs_per_day": 25}, 422, None),
    ({**TERMS, "agent_id": str(uuid.UUID(int=7))}, 422, None),
    ({**TERMS, "capabilities": ["file.write"]}, 422, None),
    ({"max_runs_per_day": 1}, 422, None),
])
async def test_terms_beyond_the_spec_or_naming_authority_are_refused(h, terms, status, reason):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=UNATTENDED_DRAFT)
    resp = await request_delegation(h, alice, agent["agent_id"], terms)
    assert resp.status_code == status, resp.text
    if reason is not None:
        assert resp.json()["error"]["details"]["reason"] == reason
    assert await _delegations(h, agent["agent_id"]) == []


async def test_an_on_demand_agent_cannot_be_delegated(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=DRAFT)
    resp = await request_delegation(h, alice, agent["agent_id"])
    assert resp.status_code == 409 and resp.json()["error"]["details"]["reason"] == "not_unattended"


async def test_with_unattended_off_nothing_can_be_delegated(make_harness):
    h = await make_harness(config=AGENTS_ON)
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=DRAFT)
    resp = await request_delegation(h, alice, agent["agent_id"])
    assert resp.status_code == 409 and resp.json()["error"]["details"]["reason"] == "unattended_unavailable"
    compiled = await h.client.post(f"{API}/compile", json=UNATTENDED_DRAFT, headers=alice.auth)
    assert compiled.json()["kind"] == "rejected" and "unattended_unavailable" in compiled.json()["reason_codes"]


async def test_another_users_agent_and_delegation_do_not_exist_for_them(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    agent = await create_agent(h, alice, draft=UNATTENDED_DRAFT)
    await grant_delegation(h, alice, agent["agent_id"])
    url = delegation_url(agent["agent_id"])
    assert (await h.client.get(url, headers=bob.auth)).status_code == 404
    assert (await request_delegation(h, bob, agent["agent_id"])).status_code == 404
    assert (await h.client.delete(url, headers=bob.auth)).status_code == 404
    [row] = await _delegations(h, agent["agent_id"])
    assert row.status == "active"


async def test_renewal_is_a_new_grant_that_supersedes_the_old_one(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=UNATTENDED_DRAFT)
    await grant_delegation(h, alice, agent["agent_id"])
    await grant_delegation(h, alice, agent["agent_id"], {**TERMS, "max_runs_per_day": 1})
    rows = sorted(await _delegations(h, agent["agent_id"]), key=lambda r: r.created_at)
    assert [(r.status, r.status_reason) for r in rows] == [("revoked", "renewed"), ("active", None)]


# ── losing it ─────────────────────────────────────────────────────────────


async def test_the_owner_revokes_without_confirmation(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=UNATTENDED_DRAFT)
    await grant_delegation(h, alice, agent["agent_id"])
    resp = await h.client.delete(delegation_url(agent["agent_id"]), headers=alice.auth)
    assert resp.status_code == 200 and resp.json()["status"] == "revoked", resp.text
    [row] = await _delegations(h, agent["agent_id"])
    assert (row.status, row.status_reason) == ("revoked", "owner_revoked") and row.revoked_at is not None
    assert "delegation_revoked" in await _notices(h, agent["agent_id"])
    shown = await h.client.get(delegation_url(agent["agent_id"]), headers=alice.auth)
    assert shown.status_code == 200 and shown.json()["status"] == "revoked"


@pytest.mark.parametrize("change,status,reason", [
    ("pause", "revoked", "paused"),
    ("delete", "revoked", "deleted"),
    ("operator_pause", "revoked", "operator_paused"),
    ("update", "invalidated", "spec_changed"),
])
async def test_anything_that_changes_the_agent_ends_its_delegation(h, change, status, reason):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=UNATTENDED_DRAFT)
    agent_id = agent["agent_id"]
    await grant_delegation(h, alice, agent_id)
    if change == "pause":
        assert (await h.client.post(f"{API}/{agent_id}/pause", json={}, headers=alice.auth)).status_code == 200
    elif change == "delete":
        first = await h.client.delete(f"{API}/{agent_id}", headers=alice.auth)
        token = first.json()["error"]["details"]["confirmation_token"]
        assert (await h.client.delete(f"{API}/{agent_id}", headers={**alice.auth, HEADER: token})).status_code == 204
    elif change == "operator_pause":
        resp = await h.client.post(f"/api/v1/admin/control/agents/{agent_id}/pause", json={"reason": "abuse"},
                                   headers=SU)
        assert resp.status_code == 200, resp.text
    else:
        compiled = await h.client.post(f"{API}/compile",
                                       json={**UNATTENDED_DRAFT, "purpose": "Summarize critical advisories only."},
                                       params={"agent_id": agent_id}, headers=alice.auth)
        assert compiled.status_code == 200 and compiled.json()["kind"] == "compiled", compiled.text
        compile_id = compiled.json()["compile_id"]
        first = await h.client.patch(f"{API}/{agent_id}", json={"compile_id": compile_id}, headers=alice.auth)
        token = first.json()["error"]["details"]["confirmation_token"]
        done = await h.client.patch(f"{API}/{agent_id}", json={"compile_id": compile_id},
                                    headers={**alice.auth, HEADER: token})
        assert done.status_code == 200, done.text
    [row] = await _delegations(h, agent_id)
    assert (row.status, row.status_reason) == (status, reason)
    if change == "pause":
        # Resuming gives the agent back, never its delegation: that needs a
        # new step-up grant.
        first = await h.client.post(f"{API}/{agent_id}/resume", json={}, headers=alice.auth)
        token = first.json()["error"]["details"]["confirmation_token"]
        assert (await h.client.post(f"{API}/{agent_id}/resume", json={}, headers={**alice.auth, HEADER: token})
                ).status_code == 200
        [row] = await _delegations(h, agent_id)
        assert row.status == "revoked"
