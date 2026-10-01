"""docs/29 §17, §22.1, §23.2 — usage attribution, the monthly budget and
export (Phase 2 H).

* Attribution: every usage event an agent run causes (each worker model call,
  each tool call, each routed model call) is joined to the run through
  `agent_run_usage` — the locked `usage_events` entity is unchanged — and the
  run's cost is the sum of its events. No agent call is free or invisible.
* Budget: a run is refused once the agent's month is spent; the per-run
  ceiling is never more than what is left of the month.
* Export: the owner's own agent — its definition, spec versions, runs,
  notebook and inbox — with no provider key, no session or confirmation token,
  no authorization internals. Owner-only; a deleted agent has nothing to export.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from server.storage.models import AgentRunRow, AgentRunUsageRow, ConfirmationToken, UsageEvent
from tests.agents.harness import AGENTS_ON, API, HEADER, create_agent, run_agent
from tests.agents.test_envelope_gate import READ_DRAFT, REPORTS
from tests.agents.test_model_routing_runs import (
    KEY_ENV,
    MODEL_DRAFT,
    Wire,
    _config,
    _routed,
    _with_model_tool_ability,
)
from tests.agents.test_notebook import MONITOR_DRAFT, put
from tests.runtime.conftest import ask, call, final


@pytest.fixture
async def h(make_harness):
    return await make_harness(config=AGENTS_ON, agent_tools=True)


async def _run(h, actor, agent_id: str) -> dict:
    resp = await run_agent(h, actor, agent_id)
    assert resp.status_code == 202, resp.text
    return resp.json()


async def _attributed(h, run_id: str) -> list[UsageEvent]:
    async with h.storage.session() as s:
        return list((await s.execute(
            select(UsageEvent).join(AgentRunUsageRow, AgentRunUsageRow.usage_id == UsageEvent.usage_id)
            .where(AgentRunUsageRow.run_id == uuid.UUID(run_id))
        )).scalars().all())


# ── attribution ────────────────────────────────────────────────────────────


async def test_every_model_and_tool_call_of_a_run_is_attributed_to_it(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    await h.grant(alice, "file.read", resource_scope=REPORTS)
    h.model.push(ask("file.read", scope=REPORTS), call("files.read", "list_directory", scope=REPORTS),
                 final("done"))
    view = await _run(h, alice, agent["agent_id"])
    events = await _attributed(h, view["run_id"])
    kinds = sorted((e.kind.value, e.tool_id) for e in events)
    assert kinds == [("model_call", None)] * 3 + [("tool_call", "files.read")]
    # Every usage event of the run's task is attributed — nothing invisible.
    assert len(events) == len(await h.rows(UsageEvent))


async def test_an_ordinary_task_is_attributed_to_no_run(h):
    alice = await h.user("alice")
    h.model.push(final("plain"))
    await h.submit(alice, "hello")
    assert len(await h.rows(UsageEvent)) == 1
    assert await h.rows(AgentRunUsageRow) == []


async def test_routed_model_spend_is_the_runs_cost(make_harness, monkeypatch):
    monkeypatch.setenv(KEY_ENV, f"TEST-ONLY-{uuid.uuid4().hex}")
    wire = Wire(usage=(5000, 2500))
    config = _config()
    config["models_as_tools"][0]["pricing"] = {"input_per_1k_tokens": 0.001, "output_per_1k_tokens": 0.002}
    h = await make_harness(config=config, agent_tools=True, transport=httpx.MockTransport(wire.handler))
    _with_model_tool_ability(h)
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=MODEL_DRAFT)
    await h.grant(alice, "model.invoke")
    h.model.push(ask("model.invoke"), _routed(), final("written"))
    view = await _run(h, alice, agent["agent_id"])
    events = await _attributed(h, view["run_id"])
    routed = [e for e in events if e.model == "writer-model"]
    assert len(routed) == 1
    [run] = await h.rows(AgentRunRow)
    assert run.cost_total == pytest.approx(sum(e.estimated_cost for e in events)) and run.cost_total > 0


# ── the monthly budget ─────────────────────────────────────────────────────


async def test_a_spent_month_refuses_new_runs(make_harness, monkeypatch):
    monkeypatch.setenv(KEY_ENV, f"TEST-ONLY-{uuid.uuid4().hex}")
    config = _config()
    config["agents"]["default_budget_per_month"] = 0.03
    h = await make_harness(config=config, agent_tools=True)
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    async with h.storage.session() as s:
        from datetime import datetime, timezone
        s.add(AgentRunRow(run_id=uuid.uuid4(), agent_id=uuid.UUID(agent["agent_id"]), owner_user_id=alice.user_id,
                          version=1, spec_hash="0" * 64, kind="on_demand", status="completed", cost_total=0.03,
                          started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc)))
        await s.commit()
    resp = await run_agent(h, alice, agent["agent_id"])
    assert resp.status_code == 429, resp.text
    assert resp.json()["error"]["details"]["reason"] == "agent_budget_exhausted"
    assert len(await h.rows(AgentRunRow)) == 1


async def test_the_run_budget_is_what_is_left_of_the_month(make_harness, monkeypatch):
    monkeypatch.setenv(KEY_ENV, f"TEST-ONLY-{uuid.uuid4().hex}")
    wire = Wire(usage=(5000, 2500))     # 0.01 per routed call
    config = _config()
    config["models_as_tools"][0]["pricing"] = {"input_per_1k_tokens": 0.001, "output_per_1k_tokens": 0.002}
    config["agents"]["default_budget_per_run"] = 0.05
    config["agents"]["default_budget_per_month"] = 0.035
    h = await make_harness(config=config, agent_tools=True, transport=httpx.MockTransport(wire.handler))
    _with_model_tool_ability(h)
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=MODEL_DRAFT)
    await h.grant(alice, "model.invoke")
    async with h.storage.session() as s:
        from datetime import datetime, timezone
        s.add(AgentRunRow(run_id=uuid.uuid4(), agent_id=uuid.UUID(agent["agent_id"]), owner_user_id=alice.user_id,
                          version=1, spec_hash="0" * 64, kind="on_demand", status="completed", cost_total=0.015,
                          started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc)))
        await s.commit()
    # 0.02 left of the month: two routed calls fit, the third does not.
    h.model.push(ask("model.invoke"), *[_routed() for _ in range(6)], final("never"))
    view = await _run(h, alice, agent["agent_id"])
    assert view["status"] == "failed" and view["failure_code"] == "budget_exceeded"
    assert len(wire.requests) == 2


# ── export ─────────────────────────────────────────────────────────────────


async def test_the_owner_exports_everything_and_no_secret(make_harness, monkeypatch):
    key = f"TEST-ONLY-writer-key-{uuid.uuid4().hex}"
    monkeypatch.setenv(KEY_ENV, key)
    h = await make_harness(config=_config(), agent_tools=True)
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=MONITOR_DRAFT)
    h.model.push(put("last_seen", "advisory-7"), final("Digest: advisory-7 is new."))
    view = await _run(h, alice, agent["agent_id"])
    resp = await h.client.get(f"{API}/{agent['agent_id']}/export", headers=alice.auth)
    assert resp.status_code == 200, resp.text
    exported = resp.json()
    assert exported["format"] == "jarvis.agent.export" and exported["format_version"] == 1
    assert exported["agent"]["agent_id"] == agent["agent_id"]
    assert [v["version"] for v in exported["spec_versions"]] == [1]
    assert exported["spec_versions"][0]["spec"]["spec_hash"] == exported["spec_versions"][0]["spec_hash"]
    assert [r["run_id"] for r in exported["runs"]] == [view["run_id"]]
    assert [(n["key"], n["value"]) for n in exported["notebook"]] == [("last_seen", "advisory-7")]
    assert [i["body"] for i in exported["inbox"]] == ["Digest: advisory-7 is new."]
    text = resp.text
    assert key not in text and "secret_ref" not in text and KEY_ENV not in text
    bearer = alice.auth["Authorization"].split(" ", 1)[1]
    assert bearer not in text
    for token in await h.rows(ConfirmationToken):
        assert token.token_hash not in text
    for internal in ("confirmation_token", "grant_id", "permission_decision", "session_id", "device_id"):
        assert internal not in text, internal
    assert json.loads(text) == exported


async def test_export_is_owner_only_and_a_deleted_agent_has_none(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    assert (await h.client.get(f"{API}/{agent['agent_id']}/export", headers=bob.auth)).status_code == 404
    first = await h.client.delete(f"{API}/{agent['agent_id']}", headers=alice.auth)
    token = first.json()["error"]["details"]["confirmation_token"]
    await h.client.delete(f"{API}/{agent['agent_id']}", headers={**alice.auth, HEADER: token})
    assert (await h.client.get(f"{API}/{agent['agent_id']}/export", headers=alice.auth)).status_code == 404
