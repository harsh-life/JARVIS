"""docs/29 §23.2, §14.3 — the operator console around agent runs, and evaluation
of agent runs after the Judge's retirement (Phase 3, slice 3C; AGENT-T26).

Agent runs are evaluated like any task by the deterministic RulesEvaluator: a
record, never a suggestion. The retired LLM Judge used to offer the agent's
owner a rewording of its purpose (`agent.purpose` candidates, docs/29 §18);
that path and its routes are gone (D-JDG-RETIRE). An owner changes their own
agent only through the ordinary, confirmed `agent.define.update`.

The operator console shows agents and runs read-only and redacted; the one
operator control is a pause, through the existing stop semantics (the
breaker's operator trip) — it stops the agent's live runs, revokes their
tokens, and holds until the operator releases it. It resumes nothing.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import update

from server.security.superuser import SUPERUSER_TOKEN_ENV
from server.storage.models import (
    AgentDefinitionRow,
    AgentRunRow,
    AgentRunTokenRow,
    AgentSpecVersionRow,
    AuditEvent,
    ImprovementCandidateRow,
    TaskEvaluation,
)
from tests.agents.harness import AGENTS_ON, API, DRAFT, HEADER, create_agent, run_agent
from tests.agents.test_envelope_gate import READ_DRAFT, REPORTS
from tests.evaluation.conftest import SU, TOKEN, drain, rules_config
from tests.runtime.conftest import ask, call, final

ADMIN = "/api/v1/admin"
BETTER = "Summarize only the critical advisories, newest first, in three lines."


@pytest.fixture
async def h(make_harness, monkeypatch):
    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, TOKEN)
    return await make_harness(config={**AGENTS_ON, **rules_config()}, agent_tools=True)


def _view(resp) -> dict:
    assert resp.status_code == 202, resp.text
    return resp.json()


async def _evaluated_run(h, actor, agent) -> dict:
    h.model.push(final("Two critical advisories."))
    view = _view(await run_agent(h, actor, agent["agent_id"]))
    await drain(h)
    return view


# ── evaluation of an agent run: a record, never a suggestion ─────────────


async def test_an_agent_run_is_evaluated_like_any_task_and_changes_nothing(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    view = await _evaluated_run(h, alice, agent)
    [evaluation] = await h.rows(TaskEvaluation, TaskEvaluation.task_id == uuid.UUID(view["task_id"]))
    assert evaluation.evaluator_id == "rules_evaluator"
    assert evaluation.owner_user_id == alice.user_id
    assert await h.rows(ImprovementCandidateRow) == []
    [spec] = await h.rows(AgentSpecVersionRow)
    assert spec.version == 1 and spec.spec_json["purpose"] == DRAFT["purpose"]


@pytest.mark.parametrize("method, suffix", [
    ("GET", "candidates"),
    ("POST", "candidates/{cid}/compile"),
    ("POST", "candidates/{cid}/dismiss"),
])
async def test_the_retired_purpose_suggestion_routes_are_gone(h, method, suffix):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    cid = uuid.uuid4()
    async with h.storage.session() as s:   # even a historical pending suggestion cannot be acted on
        s.add(_historical_candidate(h, alice, agent, cid))
        await s.commit()
    url = f"{API}/{agent['agent_id']}/" + suffix.format(cid=cid)
    resp = await h.client.request(method, url, json={} if method == "POST" else None, headers=alice.auth)
    assert resp.status_code in (404, 405), resp.text
    [row] = await h.rows(ImprovementCandidateRow)
    assert row.status == "pending"   # untouched historical data
    resp = await h.client.post(f"{ADMIN}/control/evaluation/candidates/{cid}/approve",
                               json={"reason": "looks_useful"}, headers=SU)
    assert resp.status_code in (404, 405), resp.text


def _historical_candidate(h, actor, agent, candidate_id) -> ImprovementCandidateRow:
    """A row shaped like one the retired Judge wrote, for the deletion path."""

    return ImprovementCandidateRow(
        candidate_id=candidate_id, evaluation_id=h._historical_evaluation_id, task_id=h._historical_task_id,
        source_user_id=actor.user_id, evaluator_id="llm_judge", target="agent.purpose",
        agent_id=uuid.UUID(agent["agent_id"]), proposed_value="historical rewording", expected_effect="",
        evidence=[], status="pending", created_at=datetime.now(timezone.utc),
    )


@pytest.fixture(autouse=True)
async def _historical_rows(h):
    """`improvement_candidates` rows reference an evaluation and a task. Seed
    one real task + evaluation row for historical candidates to point at."""

    alice = await h.user("seed")
    h.model.push(final("seed"))
    resp = await h.submit(alice, "seed")
    assert resp.status_code == 200, resp.text
    await drain(h)
    [evaluation] = await h.rows(TaskEvaluation)
    h._historical_task_id, h._historical_evaluation_id = evaluation.task_id, evaluation.evaluation_id
    yield


async def test_deleting_an_agent_removes_its_historical_suggestions(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    async with h.storage.session() as s:
        s.add(_historical_candidate(h, alice, agent, uuid.uuid4()))
        await s.commit()
    assert len(await h.rows(ImprovementCandidateRow)) == 1
    first = await h.client.delete(f"{API}/{agent['agent_id']}", headers=alice.auth)
    token = first.json()["error"]["details"]["confirmation_token"]
    done = await h.client.delete(f"{API}/{agent['agent_id']}", headers={**alice.auth, HEADER: token})
    assert done.status_code == 204, done.text
    assert await h.rows(ImprovementCandidateRow) == []


# ── the operator console: read-only, redacted ────────────────────────────


async def test_the_console_shows_agents_and_runs_redacted(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    a = await create_agent(h, alice)
    b = await create_agent(h, bob, draft={**DRAFT, "name": "Bob's very private agent name"})
    run = await _evaluated_run(h, alice, a)    # the rules evaluation lands before the console reads
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
