"""docs/29 §11 — the Agent Gateway around a real native run (Phase 3, slice 3A).

A native run is an ordinary task of the present owner, and it now reaches its
model and its tools through the in-process Agent Gateway exactly as an
external runtime would over the network: every request presents that
gateway's run token, a fresh nonce and its `sent_at`.

What must hold, end to end through the production runtime and engine:

* a run is issued two tokens (model, tool), bound to it, revoked when it ends;
* the tokens never reach the model, a record, a log or the owner's views;
* a token revoked or expired mid-run stops the run at its next request — the
  revocation is read fresh, never cached (AGENT-T8);
* stopping a run revokes its tokens *before* the runtime is told (AGENT-T23);
* a retried request (same nonce, same request) is answered from the ledger
  and never executed twice; a nonce reused for another request, or a stale
  request, is refused and nothing is executed;
* ordinary tasks, and a server with agents off, never touch any of it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from server.agent import runtime as agent_runtime
from server.agents.gateway import tokens as run_tokens
from server.storage.models import (
    AgentDefinitionRow,
    AgentGatewayNonceRow,
    AgentInboxItemRow,
    AgentRunRow,
    AgentRunTokenRow,
    AgentTask,
    AuditEvent,
)
from shared.schemas.agent_factory import RunTokenPurpose
from tests.agents.harness import AGENTS_ON, API, create_agent, run_agent
from tests.agents.test_envelope_gate import READ_DRAFT, REPORTS
from tests.runtime.conftest import ask, call, final


@pytest.fixture
async def h(make_harness):
    return await make_harness(config=AGENTS_ON, agent_tools=True)


@pytest.fixture
def issued(monkeypatch) -> list[str]:
    """Every run token minted, in plaintext — what the tests look for."""

    captured: list[str] = []
    original = run_tokens.new_token

    def capture() -> str:
        token = original()
        captured.append(token)
        return token

    monkeypatch.setattr(run_tokens, "new_token", capture)
    return captured


def view_of(resp) -> dict:
    assert resp.status_code == 202, resp.text
    return resp.json()


async def _audit(h, action: str) -> list[AuditEvent]:
    return await h.rows(AuditEvent, AuditEvent.action == action)


async def _tokens(h, purpose: str | None = None) -> list[AgentRunTokenRow]:
    where = [AgentRunTokenRow.purpose == purpose] if purpose else []
    return await h.rows(AgentRunTokenRow, *where)


async def _set_tokens(h, purpose: str | None = None, **values) -> None:
    query = update(AgentRunTokenRow)
    if purpose:
        query = query.where(AgentRunTokenRow.purpose == purpose)
    async with h.storage.session() as s:
        await s.execute(query.values(**values))
        await s.commit()


async def _reader(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    await h.grant(alice, "file.read", resource_scope=REPORTS)
    return alice, agent


# ── issuing, binding, ending ────────────────────────────────────────────


async def test_a_run_is_issued_two_tokens_bound_to_it_that_end_with_it(h, issued):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    h.model.push(final("done"))
    view = view_of(await run_agent(h, alice, agent["agent_id"]))
    assert view["status"] == "completed"

    [run] = await h.rows(AgentRunRow)
    rows = await _tokens(h)
    assert len(issued) == 2 and len(rows) == 2
    assert {r.purpose for r in rows} == {"model", "tool"}
    for row in rows:
        assert (row.run_id, row.agent_id, row.spec_hash) == (run.run_id, run.agent_id, run.spec_hash)
        assert row.revoked_at is not None and row.revoked_reason == "finished"
        # Short-lived: never past the run's own deadline plus 30 s.
        started = run.started_at.replace(tzinfo=timezone.utc) if run.started_at.tzinfo is None else run.started_at
        expires = row.expires_at.replace(tzinfo=timezone.utc) if row.expires_at.tzinfo is None else row.expires_at
        assert expires <= started + timedelta(seconds=h.config.agent.bounds.wall_clock_timeout_seconds + 31)
    [issue_row] = await _audit(h, "agent.token.issued")
    assert issue_row.resource == f"agentrun:{run.run_id}:model,tool"
    assert [r.resource for r in await _audit(h, "agent.token.revoked")] == [f"agentrun:{run.run_id}:finished"]
    # Each run has its own.
    h.model.push(final("again"))
    view_of(await run_agent(h, alice, agent["agent_id"]))
    assert len(issued) == 4 and len({row.token_hash for row in await _tokens(h)}) == 4


async def test_run_tokens_never_reach_the_model_the_records_or_the_owner(h, issued):
    alice, agent = await _reader(h)
    h.model.push(ask("file.read", scope=REPORTS), call("files.read", "list_directory", scope=REPORTS),
                 final("three reports"))
    view = view_of(await run_agent(h, alice, agent["agent_id"]))
    assert view["status"] == "completed" and len(h.reads.calls) == 1

    haystacks = [h.model.all_text(), json.dumps(view)]
    haystacks += [f"{a.action} {a.resource}" for a in await h.rows(AuditEvent)]
    haystacks += [t.response or "" for t in await h.rows(AgentTask)]
    haystacks += [i.body for i in await h.rows(AgentInboxItemRow)]
    haystacks += [json.dumps(n.response or {}) for n in await h.rows(AgentGatewayNonceRow)]
    for path in (f"{API}/{agent['agent_id']}/export", f"{API}/{agent['agent_id']}/runs", f"{API}/inbox"):
        resp = await h.client.get(path, headers=alice.auth)
        assert resp.status_code == 200, resp.text
        haystacks.append(resp.text)
    assert len(issued) == 2
    for token in issued:
        assert all(token not in text for text in haystacks)
    # What is stored is the hash, and only the hash.
    assert {r.token_hash for r in await _tokens(h)} == {run_tokens.token_digest(t) for t in issued}


# ── AGENT-T8 / T7: revoked or expired mid-run, read fresh ────────────────


async def test_agent_t8_a_token_revoked_mid_run_stops_its_next_request(h):
    alice, agent = await _reader(h)

    async def revoke_then_read(messages):
        await _set_tokens(h, "tool", revoked_at=datetime.now(timezone.utc), revoked_reason="breaker")
        return call("files.read", "list_directory", scope=REPORTS)

    h.model.push(ask("file.read", scope=REPORTS), revoke_then_read, final("never reached"))
    view = view_of(await run_agent(h, alice, agent["agent_id"]))
    assert view["status"] == "failed" and view["failure_code"] == "agent_unavailable"
    assert h.reads.calls == []
    [denial] = await _audit(h, "agent.gateway.denied")
    assert denial.resource.endswith(":tool:invalid_run_token:revoked")


async def test_a_model_token_revoked_mid_run_stops_before_the_next_model_call(h):
    alice = await h.user("alice")
    agent = await create_agent(h, alice)

    async def revoke_model_token(messages):
        await _set_tokens(h, "model", revoked_at=datetime.now(timezone.utc), revoked_reason="breaker")
        return call("no.such.tool", "x")

    h.model.push(revoke_model_token, final("never reached"))
    view = view_of(await run_agent(h, alice, agent["agent_id"]))
    assert view["status"] == "failed" and view["failure_code"] == "agent_unavailable"
    assert len(h.model.seen) == 1   # the second model call was never made
    [denial] = await _audit(h, "agent.gateway.denied")
    assert denial.resource.endswith(":model:invalid_run_token:revoked")


async def test_agent_t7_a_token_that_expires_mid_run_stops_it(h):
    alice, agent = await _reader(h)

    async def expire_then_read(messages):
        await _set_tokens(h, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        return call("files.read", "list_directory", scope=REPORTS)

    h.model.push(ask("file.read", scope=REPORTS), expire_then_read, final("never reached"))
    view = view_of(await run_agent(h, alice, agent["agent_id"]))
    assert view["status"] == "failed" and view["failure_code"] == "agent_unavailable"
    assert h.reads.calls == []
    [denial] = await _audit(h, "agent.gateway.denied")
    assert denial.resource.endswith(":invalid_run_token:expired")


# ── AGENT-T23: a stop revokes the tokens before the runtime hears of it ──


async def test_agent_t23_cancel_revokes_the_tokens_before_the_runtime_is_told(h, monkeypatch):
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    # No standing grant: the activation waits for the owner's confirmation.
    h.model.push(ask("file.read", scope=REPORTS))
    view = view_of(await run_agent(h, alice, agent["agent_id"]))
    assert view["status"] == "waiting", view
    assert all(row.revoked_at is None for row in await _tokens(h))

    tasks = h.app.state.agent_tasks
    seen: list[bool] = []
    original = tasks.cancel

    async def cancel(session, **kwargs):
        # What the request's own transaction holds when the runtime is told.
        rows = (await session.execute(
            select(AgentRunTokenRow).execution_options(populate_existing=True))).scalars().all()
        seen.append(bool(rows) and all(r.revoked_at is not None for r in rows))
        return await original(session, **kwargs)

    monkeypatch.setattr(tasks, "cancel", cancel)
    resp = await h.client.post(f"{API}/{agent['agent_id']}/runs/{view['run_id']}/cancel", json={},
                               headers=alice.auth)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    assert seen == [True]
    assert {r.revoked_reason for r in await _tokens(h)} == {"owner_stop"}


async def test_an_approval_after_the_agent_was_paused_performs_nothing(h):
    """The owner's approval resumes a run only through the per-step check:
    a paused agent's pending activation is never performed, whoever paused it
    (here: directly in the store, so nothing else stopped the run)."""

    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=READ_DRAFT)
    h.model.push(ask("file.read", scope=REPORTS), final("never reached"))
    view = view_of(await run_agent(h, alice, agent["agent_id"]))
    assert view["status"] == "waiting", view
    token = view["task"]["pending"]["confirmation_token"]
    async with h.storage.session() as s:
        await s.execute(update(AgentDefinitionRow).values(status="paused"))
        await s.commit()

    resp = await h.confirm(alice, view["task_id"], token)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"]["failure_code"] == "agent_unavailable"
    assert await _audit(h, "capability.activated") == []
    [run] = await h.rows(AgentRunRow)
    assert (run.status, run.failure_code) == ("failed", "agent_unavailable")


# ── replay and idempotency inside a run ──────────────────────────────────


async def test_a_retried_tool_request_is_answered_from_the_ledger_not_run_again(h, monkeypatch):
    """The same request, the same nonce: what a runtime sends when it retries.
    The second is answered with the first's stored observation."""

    original = agent_runtime.gateway_request
    sent: dict[str, object] = {}

    def retry_same(binding, purpose, digest):
        request = original(binding, purpose, digest)
        if purpose is RunTokenPurpose.TOOL:
            return sent.setdefault(digest, request)
        return request

    monkeypatch.setattr(agent_runtime, "gateway_request", retry_same)
    alice, agent = await _reader(h)
    listing = call("files.read", "list_directory", scope=REPORTS)
    h.model.push(ask("file.read", scope=REPORTS), listing, listing, final("three reports"))
    view = view_of(await run_agent(h, alice, agent["agent_id"]))
    assert view["status"] == "completed", view
    assert len(h.reads.calls) == 1
    assert len(await _audit(h, "agent.tool.executed")) == 1
    [ledger] = [n for n in await h.rows(AgentGatewayNonceRow) if (n.response or {}).get("observations")]
    assert ledger.status == "done"


async def test_a_nonce_reused_for_another_request_is_refused_as_a_replay(h, monkeypatch):
    original = agent_runtime.gateway_request
    first: dict[str, str] = {}

    def reuse_nonce(binding, purpose, digest):
        request = original(binding, purpose, digest)
        if purpose is RunTokenPurpose.TOOL:
            return request.model_copy(update={"request_nonce": first.setdefault("nonce", request.request_nonce)})
        return request

    monkeypatch.setattr(agent_runtime, "gateway_request", reuse_nonce)
    alice, agent = await _reader(h)
    h.model.push(ask("file.read", scope=REPORTS), call("files.read", "list_directory", scope=REPORTS),
                 call("files.read", "list_directory", scope=REPORTS, args={"page": 2}), final("done"))
    view = view_of(await run_agent(h, alice, agent["agent_id"]))
    assert view["status"] == "completed", view
    assert len(h.reads.calls) == 1
    assert "refused this request (replay)" in h.model.all_text()
    [denial] = await _audit(h, "agent.gateway.denied")
    assert denial.resource.endswith(":tool:replay:nonce_reused")


async def test_a_stale_tool_request_is_refused_and_not_executed(h, monkeypatch):
    original = agent_runtime.gateway_request

    def late(binding, purpose, digest):
        request = original(binding, purpose, digest)
        if purpose is RunTokenPurpose.TOOL:
            return request.model_copy(update={"sent_at": request.sent_at - timedelta(minutes=5)})
        return request

    monkeypatch.setattr(agent_runtime, "gateway_request", late)
    alice, agent = await _reader(h)
    h.model.push(ask("file.read", scope=REPORTS), call("files.read", "list_directory", scope=REPORTS),
                 final("done"))
    view = view_of(await run_agent(h, alice, agent["agent_id"]))
    assert view["status"] == "completed", view
    assert h.reads.calls == []
    assert "refused this request (stale_request)" in h.model.all_text()


# ── nothing changes outside agent runs ───────────────────────────────────


async def test_an_ordinary_task_never_touches_the_gateway(h):
    alice = await h.user("alice")
    await h.grant(alice, "file.read", resource_scope=REPORTS)
    h.model.push(ask("file.read", scope=REPORTS), call("files.read", "list_directory", scope=REPORTS),
                 final("plain"))
    resp = await h.submit(alice, "list my reports")
    assert resp.status_code == 200 and resp.json()["status"] == "completed", resp.text
    assert len(h.reads.calls) == 1
    assert await _tokens(h) == [] and await h.rows(AgentGatewayNonceRow) == []


async def test_with_agents_disabled_nothing_is_issued(make_harness):
    h = await make_harness()
    alice = await h.user("alice")
    h.model.push(final("plain"))
    assert (await h.submit(alice, "hello")).status_code == 200
    assert await h.rows(AgentRunTokenRow) == [] and await h.rows(AgentGatewayNonceRow) == []
