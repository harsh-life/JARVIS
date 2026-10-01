"""docs/29 §18, §23.2, §26 — the Judge and the operator console around agent
runs (Phase 3, slice 3C; AGENT-T26).

The Judge observes an agent run like any task, now with the run's attribution
(agent, run, version); it may suggest a better wording of *that agent's*
purpose, and nothing else about an agent. The suggestion goes to the agent's
owner only; it changes nothing until the owner compiles it into an update and
confirms that update — the ordinary, consequential `agent.define.update`. No
superuser approval can apply it, and the Judge can never touch an agent's
envelope, budget, trigger, runtime, model, or anything that grants.

The operator console shows agents and runs read-only and redacted; the one
operator control is a pause, through the existing stop semantics (the
breaker's operator trip) — it stops the agent's live runs, revokes their
tokens, and holds until the operator releases it. It resumes nothing.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import update

from server.security.superuser import SUPERUSER_TOKEN_ENV
from server.storage.models import (
    AgentDefinitionRow,
    AgentRunRow,
    AgentRunTokenRow,
    AgentSpecVersionRow,
    AgentTask,
    AuditEvent,
    ConfigVersion,
    ImprovementCandidateRow,
    TaskEvaluation,
)
from tests.agents.harness import AGENTS_ON, API, DRAFT, HEADER, create_agent, run_agent
from tests.agents.test_envelope_gate import READ_DRAFT, REPORTS
from tests.evaluation.conftest import SU, TOKEN, drain, judge_config, verdict
from tests.runtime.conftest import ScriptedModel, ask, call, final

ADMIN = "/api/v1/admin"
BETTER = "Summarize only the critical advisories, newest first, in three lines."


@pytest.fixture
async def h(make_harness, monkeypatch):
    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, TOKEN)
    judge = ScriptedModel("scripted-judge")
    harness = await make_harness(config={**AGENTS_ON, **judge_config()}, agent_tools=True,
                                 models={"scripted-judge": judge})
    harness.judge = judge
    return harness


def _view(resp) -> dict:
    assert resp.status_code == 202, resp.text
    return resp.json()


async def _judged_run(h, actor, agent, *candidates) -> dict:
    h.judge.push(verdict(quality=0.6, improvement_candidates=list(candidates)))
    h.model.push(final("Two critical advisories."))
    view = _view(await run_agent(h, actor, agent["agent_id"]))
    await drain(h)
    return view


def _purpose(text: str = BETTER, **extra) -> dict:
    return {"target": "agent.purpose", "proposed_change": text, "expected_effect": "shorter digests", **extra}


async def _candidates(h, actor, agent_id: str):
    return await h.client.get(f"{API}/{agent_id}/candidates", headers=actor.auth)


# ── the Judge observes, with the run's attribution ───────────────────────


async def test_the_judge_sees_an_agent_run_with_its_attribution(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    view = await _judged_run(h, alice, agent)
    [evaluation] = await h.rows(TaskEvaluation)
    assert str(evaluation.task_id) == view["task_id"]
    record = h.judge.all_text()
    assert f'"agent_id": "{agent["agent_id"]}"' in record and f'"run_id": "{view["run_id"]}"' in record
    assert '"version": 1' in record
    assert "agent.purpose" in record           # offered as a target, for agent runs only


async def test_an_ordinary_task_carries_no_agent_attribution(h):
    alice = await h.user("alice")
    h.judge.push(verdict(quality=0.9))
    h.model.push(final("plain"))
    assert (await h.submit(alice, "hello")).status_code == 200
    await drain(h)
    record = h.judge.all_text()
    assert "agent_run" not in record and "agent.purpose" not in record


# ── agent.purpose: owner-scoped, applied only by the owner's update ──────


async def test_a_purpose_candidate_goes_to_the_agents_owner_and_changes_nothing(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    agent = await create_agent(h, alice)
    view = await _judged_run(h, alice, agent, _purpose())
    [row] = await h.rows(ImprovementCandidateRow)
    assert (row.target, str(row.agent_id), row.status) == ("agent.purpose", agent["agent_id"], "pending")

    listed = await _candidates(h, alice, agent["agent_id"])
    assert listed.status_code == 200, listed.text
    [item] = listed.json()["items"]
    assert item["proposed_purpose"] == BETTER and item["status"] == "pending"
    assert item["run_id"] == view["run_id"]
    # Nobody else sees it; it changed nothing about the agent.
    assert (await _candidates(h, bob, agent["agent_id"])).status_code == 404
    [spec] = await h.rows(AgentSpecVersionRow)
    assert spec.version == 1 and spec.spec_json["purpose"] == DRAFT["purpose"]


async def test_the_owner_applies_a_candidate_only_through_a_confirmed_update(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    await _judged_run(h, alice, agent, _purpose())
    [row] = await h.rows(ImprovementCandidateRow)
    resp = await h.client.post(f"{API}/{agent['agent_id']}/candidates/{row.candidate_id}/compile", json={},
                               headers=alice.auth)
    assert resp.status_code == 200, resp.text
    outcome = resp.json()
    assert outcome["kind"] == "compiled", outcome
    # Compiling is not applying: still version 1.
    assert len(await h.rows(AgentSpecVersionRow)) == 1
    body = {"compile_id": outcome["compile_id"]}
    first = await h.client.patch(f"{API}/{agent['agent_id']}", json=body, headers=alice.auth)
    assert first.status_code == 403, first.text            # consequential: the owner confirms
    token = first.json()["error"]["details"]["confirmation_token"]
    done = await h.client.patch(f"{API}/{agent['agent_id']}", json=body, headers={**alice.auth, HEADER: token})
    assert done.status_code == 200, done.text
    v1, v2 = sorted(await h.rows(AgentSpecVersionRow), key=lambda r: r.version)
    assert v2.spec_json["purpose"] == BETTER
    # Only the purpose changed: never the envelope, budget, trigger, runtime or model.
    for field in ("envelope_ceiling", "risk_ceiling", "run_mode", "budget", "bounds", "trigger", "outputs",
                  "hydration", "notebook_enabled", "sources"):
        assert v2.spec_json[field] == v1.spec_json[field], field
    for field in ("runtime_id", "model_profile_id", "model_profile_version", "template_id"):
        assert v2.spec_json["selection"][field] == v1.spec_json["selection"][field], field
    [row] = await h.rows(ImprovementCandidateRow)
    assert row.status == "approved" and row.decided_by == "owner"


async def test_a_candidate_that_would_change_more_than_the_purpose_is_refused(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    await _judged_run(h, alice, agent, _purpose())
    [row] = await h.rows(ImprovementCandidateRow)
    # The operator has since raised the agents' default budget: a recompile
    # would carry a different budget, which no Judge suggestion may bring in.
    factory = h.app.state.agent_factory._factory
    config = factory._config
    factory._config = config.model_copy(update={"agents": config.agents.model_copy(
        update={"default_budget_per_run": 0.5, "default_budget_per_month": 5.0})})
    resp = await h.client.post(f"{API}/{agent['agent_id']}/candidates/{row.candidate_id}/compile", json={},
                               headers=alice.auth)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"]["reason"] == "changes_more_than_purpose"
    assert len(await h.rows(AgentSpecVersionRow)) == 1


async def test_the_owner_can_dismiss_a_candidate(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    agent = await create_agent(h, alice)
    await _judged_run(h, alice, agent, _purpose())
    [row] = await h.rows(ImprovementCandidateRow)
    url = f"{API}/{agent['agent_id']}/candidates/{row.candidate_id}"
    assert (await h.client.post(f"{url}/dismiss", json={}, headers=bob.auth)).status_code == 404
    assert (await h.client.post(f"{url}/dismiss", json={}, headers=alice.auth)).status_code == 200
    assert (await h.client.post(f"{url}/compile", json={}, headers=alice.auth)).status_code == 409
    [row] = await h.rows(ImprovementCandidateRow)
    assert row.status == "rejected" and row.decided_by == "owner"


async def test_a_candidate_is_decided_only_through_its_own_agent(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    other = await create_agent(h, alice, draft={**DRAFT, "name": "Second digest"})
    await _judged_run(h, alice, agent, _purpose())
    [row] = await h.rows(ImprovementCandidateRow)
    for action in ("compile", "dismiss"):
        resp = await h.client.post(f"{API}/{other['agent_id']}/candidates/{row.candidate_id}/{action}", json={},
                                   headers=alice.auth)
        assert resp.status_code == 404, resp.text
    assert (await _candidates(h, alice, other["agent_id"])).json()["items"] == []
    [row] = await h.rows(ImprovementCandidateRow)
    assert row.status == "pending"


async def test_deleting_an_agent_removes_its_candidates(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    await _judged_run(h, alice, agent, _purpose())
    first = await h.client.delete(f"{API}/{agent['agent_id']}", headers=alice.auth)
    token = first.json()["error"]["details"]["confirmation_token"]
    done = await h.client.delete(f"{API}/{agent['agent_id']}", headers={**alice.auth, HEADER: token})
    assert done.status_code == 204, done.text
    assert await h.rows(ImprovementCandidateRow) == []


@pytest.mark.parametrize("target", ["agent.envelope", "agent.budget", "agent.trigger", "agent.runtime",
                                    "agent.model", "agent.delegation", "agent.selection", "agent.purpose:other",
                                    "agent.capabilities", "agent.owner"])
async def test_agent_t26_the_judge_cannot_target_an_agents_authority(h, target):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    await _judged_run(h, alice, agent, {"target": target, "proposed_change": "x"})
    assert await h.rows(ImprovementCandidateRow) == []
    [refused] = await h.rows(AuditEvent, AuditEvent.action == "improvement.candidate.refused")
    if target == "agent.purpose:other":
        assert ":bad_subject:" in refused.resource         # the purpose of *this* agent, no other
    else:
        assert refused.resource.endswith(":forbidden_target:agent_authority"), refused.resource


async def test_a_purpose_candidate_from_an_ordinary_task_is_refused(h):
    alice = await h.user("alice")
    h.judge.push(verdict(quality=0.5, improvement_candidates=[_purpose()]))
    h.model.push(final("plain"))
    assert (await h.submit(alice, "hello")).status_code == 200
    await drain(h)
    assert await h.rows(ImprovementCandidateRow) == []
    [refused] = await h.rows(AuditEvent, AuditEvent.action == "improvement.candidate.refused")
    assert ":not_an_agent_run:" in refused.resource


async def test_agent_t26_no_superuser_approval_can_apply_a_purpose_candidate(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    await _judged_run(h, alice, agent, _purpose())
    [row] = await h.rows(ImprovementCandidateRow)
    resp = await h.client.post(f"{ADMIN}/control/evaluation/candidates/{row.candidate_id}/approve",
                               json={"reason": "looks_good"}, headers=SU)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["details"]["refusal"] == "owner_scoped"
    assert await h.rows(ConfigVersion) == []
    [row] = await h.rows(ImprovementCandidateRow)
    assert row.status == "pending"
    [spec] = await h.rows(AgentSpecVersionRow)
    assert spec.version == 1


async def test_a_purpose_candidate_with_a_secret_is_refused(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    planted = "Use key sk-" + "proj-PLANTEDsecretVALUE0123456789abcd when needed."   # split: the repo secret scan
    await _judged_run(h, alice, agent, _purpose(planted))
    assert await h.rows(ImprovementCandidateRow) == []


# ── the operator console: read-only, redacted ────────────────────────────


async def test_the_console_shows_agents_and_runs_redacted(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    a = await create_agent(h, alice)
    b = await create_agent(h, bob, draft={**DRAFT, "name": "Bob's very private agent name"})
    run = await _judged_run(h, alice, a)       # the Judge's evaluation lands before the console reads
    resp = await h.client.get(f"{ADMIN}/agents", headers=SU)
    assert resp.status_code == 200, resp.text
    assert resp.json()["view"] == "agents"
    page = resp.json()["data"]
    items = {item["agent_id"]: item for item in page["agents"]}
    assert set(items) == {a["agent_id"], b["agent_id"]}
    mine = items[a["agent_id"]]
    assert mine["owner_user_id"] == str(alice.user_id) and mine["status"] == "active"
    assert mine["current_version"] == 1 and len(mine["spec_hash"]) == 64
    assert (mine["runtime_id"], mine["model_profile_id"]) == ("native", "general-agentic")
    assert mine["runs"]["total"] == 1 and mine["runs"]["live"] == 0
    assert mine["last_run"]["run_id"] == run["run_id"] and mine["last_run"]["status"] == "completed"
    assert mine["last_run"]["evaluation"] is not None
    assert set(mine["budget"]) == {"per_run", "per_month", "month_spent"}
    assert mine["control"] == {"operator_paused": False, "live_tokens": 0}
    assert page["counts"]["by_status"] == {"active": 2}
    text = resp.text
    for private in ("Bob's very private agent name", DRAFT["name"], DRAFT["purpose"], "Two critical"):
        assert private not in text
    for row in await h.rows(AgentRunTokenRow):
        assert row.token_hash not in text
    assert items[b["agent_id"]]["name"] == {"redacted": True, "chars": len("Bob's very private agent name")}


async def test_no_ordinary_credential_reaches_the_agents_console_or_control(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    assert (await h.client.get(f"{ADMIN}/agents", headers=alice.auth)).status_code in (401, 403)
    for path in (f"{ADMIN}/control/agents/{agent['agent_id']}/pause",
                 f"{ADMIN}/control/agents/{agent['agent_id']}/release"):
        for headers in (alice.auth, {"Authorization": f"Superuser {alice.token}"}, {}):
            resp = await h.client.post(path, json={"reason": "abuse"}, headers=headers)
            assert resp.status_code in (401, 403), (path, resp.text)
    [row] = await h.rows(AgentDefinitionRow)
    assert row.status == "active"


# ── the operator's pause: the existing stop semantics ────────────────────


async def test_an_operator_pause_stops_a_waiting_run_and_holds(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    h.model.push(ask("file.read", scope=REPORTS))
    view = _view(await run_agent(h, alice, agent["agent_id"]))
    assert view["status"] == "waiting"
    token = view["task"]["pending"]["confirmation_token"]

    resp = await h.client.post(f"{ADMIN}/control/agents/{agent['agent_id']}/pause", json={"reason": "abuse"},
                               headers=SU)
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["status"] == "paused" and report["stopped"] == [view["task_id"]]
    [run] = await h.rows(AgentRunRow)
    assert (run.status, run.failure_code) == ("failed", "emergency_stop")
    assert {t.revoked_reason for t in await h.rows(AgentRunTokenRow)} == {"operator_paused"}
    [definition] = await h.rows(AgentDefinitionRow)
    assert (definition.status, definition.status_reason) == ("paused", "operator_paused")
    # The paused action can never be approved now.
    assert (await h.confirm(alice, view["task_id"], token)).status_code in (404, 409)
    assert await h.rows(AuditEvent, AuditEvent.action == "capability.activated") == []
    # It holds: the owner can neither run nor resume it.
    assert (await run_agent(h, alice, agent["agent_id"])).status_code == 409
    resumed = await h.client.post(f"{API}/{agent['agent_id']}/resume", json={}, headers=alice.auth)
    assert resumed.status_code == 409 and resumed.json()["error"]["details"]["reason"] == "operator_paused"
    [audit] = await h.rows(AuditEvent, AuditEvent.action == "control.agent.paused")
    assert audit.resource == f"control:agent:pause:{agent['agent_id']}:abuse"

    # Released: still paused — the owner decides, with confirmation, to resume.
    released = await h.client.post(f"{ADMIN}/control/agents/{agent['agent_id']}/release",
                                   json={"reason": "reviewed"}, headers=SU)
    assert released.status_code == 200 and released.json()["status"] == "paused", released.text
    first = await h.client.post(f"{API}/{agent['agent_id']}/resume", json={}, headers=alice.auth)
    assert first.status_code == 403, first.text
    token = first.json()["error"]["details"]["confirmation_token"]
    again = await h.client.post(f"{API}/{agent['agent_id']}/resume", json={}, headers={**alice.auth, HEADER: token})
    assert again.status_code == 200 and again.json()["status"] == "active", again.text


async def test_an_operator_pause_reaches_a_running_run_at_its_next_step(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    await h.grant(alice, "file.read", resource_scope=REPORTS)
    state: dict = {}

    async def operator_pauses(messages):
        state["pause"] = asyncio.ensure_future(h.client.post(
            f"{ADMIN}/control/agents/{agent['agent_id']}/pause", json={"reason": "abuse"}, headers=SU))
        await asyncio.sleep(0.05)   # the trip lands in memory before any store write
        return call("files.read", "list_directory", scope=REPORTS)

    h.model.push(ask("file.read", scope=REPORTS), operator_pauses, final("never reached"))
    view = _view(await run_agent(h, alice, agent["agent_id"]))
    resp = await state["pause"]
    assert resp.status_code == 200, resp.text
    assert view["status"] == "failed" and view["failure_code"] == "emergency_stop", view
    assert h.reads.calls == []
    assert resp.json()["signalled"] == [view["task_id"]]
    [definition] = await h.rows(AgentDefinitionRow)
    assert definition.status_reason == "operator_paused"


async def test_an_operator_pause_before_the_resume_is_confirmed_holds(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    assert (await h.client.post(f"{API}/{agent['agent_id']}/pause", json={}, headers=alice.auth)).status_code == 200
    first = await h.client.post(f"{API}/{agent['agent_id']}/resume", json={}, headers=alice.auth)
    assert first.status_code == 403, first.text
    token = first.json()["error"]["details"]["confirmation_token"]
    paused = await h.client.post(f"{ADMIN}/control/agents/{agent['agent_id']}/pause", json={"reason": "abuse"},
                                 headers=SU)
    assert paused.status_code == 200, paused.text
    late = await h.client.post(f"{API}/{agent['agent_id']}/resume", json={}, headers={**alice.auth, HEADER: token})
    assert late.status_code == 409 and late.json()["error"]["details"]["reason"] == "operator_paused", late.text
    [definition] = await h.rows(AgentDefinitionRow)
    assert (definition.status, definition.status_reason) == ("paused", "operator_paused")


async def test_an_operator_pause_landing_while_the_resume_is_confirmed_holds(h, monkeypatch):
    """The hold is re-read after the engine's decision, not only before it."""

    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    assert (await h.client.post(f"{API}/{agent['agent_id']}/pause", json={}, headers=alice.auth)).status_code == 200
    first = await h.client.post(f"{API}/{agent['agent_id']}/resume", json={}, headers=alice.auth)
    token = first.json()["error"]["details"]["confirmation_token"]
    facade = h.app.state.agent_factory
    decided = facade._confirm_or_refuse

    async def the_operator_lands(session, outcome, **kwargs):
        await decided(session, outcome, **kwargs)
        await session.execute(update(AgentDefinitionRow).values(status_reason="operator_paused"))

    monkeypatch.setattr(facade, "_confirm_or_refuse", the_operator_lands)
    late = await h.client.post(f"{API}/{agent['agent_id']}/resume", json={}, headers={**alice.auth, HEADER: token})
    assert late.status_code == 409 and late.json()["error"]["details"]["reason"] == "operator_paused", late.text
    [definition] = await h.rows(AgentDefinitionRow)
    assert definition.status == "paused"


async def test_an_operator_pause_holds_through_the_owners_re_approval(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    async with h.storage.session() as session:
        await session.execute(update(AgentDefinitionRow).values(status="needs_reapproval",
                                                                status_reason="template_changed"))
        await session.commit()
    paused = await h.client.post(f"{ADMIN}/control/agents/{agent['agent_id']}/pause", json={"reason": "abuse"},
                                 headers=SU)
    assert paused.status_code == 200 and paused.json()["status"] == "paused", paused.text
    # The owner's re-approved update is a new version — not a way past the hold.
    compiled = await h.client.post(f"{API}/compile", params={"agent_id": agent["agent_id"]},
                                   json={**DRAFT, "purpose": BETTER}, headers=alice.auth)
    assert compiled.json()["kind"] == "compiled", compiled.text
    body = {"compile_id": compiled.json()["compile_id"]}
    first = await h.client.patch(f"{API}/{agent['agent_id']}", json=body, headers=alice.auth)
    token = first.json()["error"]["details"]["confirmation_token"]
    done = await h.client.patch(f"{API}/{agent['agent_id']}", json=body, headers={**alice.auth, HEADER: token})
    assert done.status_code == 200, done.text
    [definition] = await h.rows(AgentDefinitionRow)
    assert (definition.status, definition.status_reason, definition.current_version) == \
        ("paused", "operator_paused", 2)
    assert (await run_agent(h, alice, agent["agent_id"])).status_code == 409


async def test_an_operator_pause_of_an_unknown_agent_is_not_found(h):
    resp = await h.client.post(f"{ADMIN}/control/agents/{uuid.uuid4()}/pause", json={"reason": "abuse"},
                               headers=SU)
    assert resp.status_code == 404, resp.text


async def test_the_judges_stop_request_stops_an_agent_run_and_nothing_more(make_harness, monkeypatch):
    """A live-window Judge may request a stop through the breaker (19 §6) —
    the run stops; the Judge gains nothing: the agent is not changed, no grant,
    no token, no approval."""

    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, TOKEN)
    judge = ScriptedModel("scripted-judge")
    h = await make_harness(config={**AGENTS_ON, **judge_config(may_request_stop=True,
                                                                live={"enabled": True, "every_n_steps": 1})},
                           agent_tools=True, models={"scripted-judge": judge})
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    await h.grant(alice, "file.read", resource_scope=REPORTS)
    judge.push(verdict(anomaly="stop_requested", anomaly_reason="looping"))

    async def let_the_judge_run(messages):
        await drain(h)
        return call("files.read", "list_directory", scope=REPORTS)

    listing = call("files.read", "list_directory", scope=REPORTS)
    h.model.push(ask("file.read", scope=REPORTS), listing, let_the_judge_run, final("never reached"))
    view = _view(await run_agent(h, alice, agent["agent_id"]))
    assert view["status"] == "failed" and view["failure_code"] == "emergency_stop", view
    [definition] = await h.rows(AgentDefinitionRow)
    assert definition.status == "active" and definition.current_version == 1
    assert all(t.revoked_at is not None for t in await h.rows(AgentRunTokenRow))
