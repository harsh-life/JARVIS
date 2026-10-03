"""Every way a Browser Use run ends — Phase 6 slices 6E/6F.

docs/29 §11.3, §14, §25.2; OD-AF-11/12. A contained run is stopped by
JARVIS, never by the runtime's say-so, and every stop is the same kill path:
the run's tokens are revoked first (AGENT-T23 — from then on the gateway
admits nothing of this run, whatever the container is still doing), the run
and its task are closed, then the container is stopped and removed, the
sockets closed and the workspace deleted.

Who stops it, and how it reads afterwards:

* the owner — stop, pause, delete → `cancelled` (`owner_stop`, `deleted`);
* the operator — task/user stop, agent pause, global stop → `failed`
  (`emergency_stop`), exactly as a native run;
* the system — the deadline (`wall_clock_timeout`), a gateway ceiling the
  run hit (`max_model_calls`, `budget_exceeded`), a token revoked elsewhere
  (`token_revoked`), a container that died (`runtime_crashed`), a restart
  (`interrupted`) → `failed`.

The fake engine of `test_browser_runs.py` stands in for Podman + gVisor;
`tests/agents/test_browser_live.py` and `tests/execution/test_containers_live.py`
prove the real kill path.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import update

from server.agents.browser import BROWSER_CAPABILITY
from server.composition.browser_runs import run_ending_refusal
from server.composition.containers import ContainerReconciler
from server.security.superuser import SUPERUSER_TOKEN_ENV
from server.storage.models import (
    AgentInboxItemRow,
    AgentRunRow,
    AgentRunTokenRow,
    AgentRunUsageRow,
    AgentTask,
    AuditEvent,
    CapabilityGrant,
    UsageEvent,
)
from tests.agents.harness import API, HEADER, UNATTENDED_ON, create_agent, request_delegation, run_agent
from tests.agents.test_browser_runs import (
    BROWSER_DRAFT,
    _browser_harness,
    _wait_finished,
    browser_config,
    install_fake_runtime,
)


@pytest.fixture
def fake_runtime(monkeypatch):
    return install_fake_runtime(monkeypatch)


ADMIN = "/api/v1/admin"
SU_TOKEN = "TEST-ONLY-superuser-credential-0123456789abcdef"
SU = {"Authorization": f"Superuser {SU_TOKEN}"}


# ── a container that keeps going until it is stopped ───────────────────────


class Hang:
    """A runtime that makes one model call, then never finishes on its own."""

    def __init__(self, runtime, *, before=None) -> None:
        self.runtime = runtime
        self.before = before
        self.started = asyncio.Event()
        self.reply: int | None = None
        self.stopped_from_outside = False
        self.supervisor: asyncio.Task | None = None

    async def __call__(self, spec) -> int:
        if self.before is not None:
            await self.before(spec)
        try:
            self.reply = (await self.runtime.model_call(spec)).status_code
            self.started.set()
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.stopped_from_outside = True
            raise
        finally:
            self.started.set()
        return 0


async def _admits_nothing(h, fake_runtime, spec) -> None:
    """The run's gateway admits nothing more: refused, or its socket already
    gone — and the model is never reached."""

    calls = len(h.model.seen)
    try:
        reply = await fake_runtime.model_call(spec)
    except httpx.TransportError:   # the socket already gone, before or during the call
        pass
    else:
        assert reply.status_code in (401, 409), reply.text
        assert reply.json()["error"]["code"] in ("invalid_run_token", "run_not_running")
    assert len(h.model.seen) == calls


async def _start_hanging(h, alice, agent, fake_runtime, *, before=None) -> tuple[uuid.UUID, Hang]:
    await h.grant(alice, BROWSER_CAPABILITY)
    hang = Hang(fake_runtime, before=before)
    fake_runtime.behaviour = hang
    h.model.push(*["still looking"] * 4)
    resp = await run_agent(h, alice, agent["agent_id"])
    assert resp.status_code == 202, resp.text
    await asyncio.wait_for(hang.started.wait(), 10)
    run_id = uuid.UUID(resp.json()["run_id"])
    live = h.app.state.agent_factory.factory.browser_runs.live.get(run_id)
    hang.supervisor = live.supervisor if live is not None else None
    return run_id, hang


async def _assert_torn_down(h, fake_runtime, run_id: uuid.UUID, hang: Hang) -> None:
    # A stop closes the run at once; the supervisor's cleanup follows.
    if hang.supervisor is not None:
        await asyncio.wait_for(asyncio.shield(hang.supervisor), 10)
    name = f"jarvis-run-{run_id}"
    assert name in fake_runtime.stopped and hang.stopped_from_outside
    [spec] = [s for s in fake_runtime.started if s.run_id == run_id]
    assert not Path(spec.socket_dir).exists() and not Path(spec.scratch_dir).exists()
    tokens = await h.rows(AgentRunTokenRow, AgentRunTokenRow.run_id == run_id)
    assert tokens and all(t.revoked_at is not None for t in tokens)
    [row] = await h.rows(AgentRunRow, AgentRunRow.run_id == run_id)
    [task] = await h.rows(AgentTask, AgentTask.task_id == row.task_id)
    assert task.finished_at is not None and task.status in ("cancelled", "failed")
    # The task-scoped activation made from the owner's grant is gone too.
    active = [g for g in await h.rows(CapabilityGrant, CapabilityGrant.principal_id == row.task_id)
              if g.revoked_at is None]
    assert active == []
    assert h.app.state.agent_factory.factory.browser_runs.live == {}


# ── the owner ──────────────────────────────────────────────────────────────


async def test_the_owners_stop_revokes_the_tokens_at_once_and_cancels(make_harness, tmp_path, fake_runtime) -> None:
    h, alice, agent = await _browser_harness(make_harness, tmp_path, fake_runtime)
    run_id, hang = await _start_hanging(h, alice, agent, fake_runtime)
    [spec] = fake_runtime.started
    fake_runtime.stop_delay = 0.5                 # a runtime slow to die: SIGTERM ignored for a while

    stopped = await h.client.post(f"{API}/{agent['agent_id']}/runs/{run_id}/cancel", json={}, headers=alice.auth)
    assert stopped.status_code == 200, stopped.text
    # AGENT-T23: the container may still be alive; the gateway admits nothing of it.
    await _admits_nothing(h, fake_runtime, spec)

    row = await _wait_finished(h, str(run_id))
    assert (row.status, row.failure_code) == ("cancelled", "owner_stop")
    await _assert_torn_down(h, fake_runtime, run_id, hang)
    actions = [e.action for e in await h.rows(AuditEvent)]
    assert "agent.run.cancelled" in actions and "agent.token.revoked" in actions


async def test_pausing_the_agent_stops_its_contained_run(make_harness, tmp_path, fake_runtime) -> None:
    h, alice, agent = await _browser_harness(make_harness, tmp_path, fake_runtime)
    run_id, hang = await _start_hanging(h, alice, agent, fake_runtime)
    assert (await h.client.post(f"{API}/{agent['agent_id']}/pause", json={}, headers=alice.auth)).status_code == 200
    row = await _wait_finished(h, str(run_id))
    assert (row.status, row.failure_code) == ("cancelled", "owner_stop")
    await _assert_torn_down(h, fake_runtime, run_id, hang)


async def test_deleting_the_agent_stops_its_contained_run(make_harness, tmp_path, fake_runtime) -> None:
    h, alice, agent = await _browser_harness(make_harness, tmp_path, fake_runtime)
    run_id, hang = await _start_hanging(h, alice, agent, fake_runtime)
    first = await h.client.delete(f"{API}/{agent['agent_id']}", headers=alice.auth)
    token = first.json()["error"]["details"]["confirmation_token"]
    done = await h.client.delete(f"{API}/{agent['agent_id']}", headers={**alice.auth, HEADER: token})
    assert done.status_code in (200, 204), done.text
    row = await _wait_finished(h, str(run_id))
    assert (row.status, row.failure_code) == ("cancelled", "deleted")
    await _assert_torn_down(h, fake_runtime, run_id, hang)


# ── the operator ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("how", ["task", "user", "agent_pause", "global"])
async def test_every_operator_stop_reaches_a_contained_run(make_harness, tmp_path, fake_runtime, monkeypatch,
                                                           how) -> None:
    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, SU_TOKEN)
    h, alice, agent = await _browser_harness(make_harness, tmp_path, fake_runtime)
    run_id, hang = await _start_hanging(h, alice, agent, fake_runtime)
    [row] = await h.rows(AgentRunRow, AgentRunRow.run_id == run_id)
    if how == "task":
        resp = await h.client.post(f"{ADMIN}/control/stop", headers=SU, json={
            "scope": "task", "target_id": str(row.task_id), "reason": "incident"})
    elif how == "user":
        resp = await h.client.post(f"{ADMIN}/control/stop", headers=SU, json={
            "scope": "user", "target_id": str(alice.user_id), "reason": "incident"})
    elif how == "agent_pause":
        resp = await h.client.post(f"{ADMIN}/control/agents/{agent['agent_id']}/pause", headers=SU,
                                   json={"reason": "abuse"})
    else:
        resp = await h.client.post(f"{ADMIN}/control/global-stop", headers=SU, json={"reason": "incident"})
    assert resp.status_code == 200, resp.text
    # Reached directly, in the stop's own transaction — not at the next poll.
    [spec] = fake_runtime.started
    await _admits_nothing(h, fake_runtime, spec)
    if how == "task":
        assert str(row.task_id) in resp.json()["tasks"]["stopped"]
    row = await _wait_finished(h, str(run_id))
    assert (row.status, row.failure_code) == ("failed", "emergency_stop")
    await _assert_torn_down(h, fake_runtime, run_id, hang)


async def test_the_global_latch_tripped_by_any_path_stops_a_contained_run(make_harness, tmp_path,
                                                                        fake_runtime) -> None:
    h, alice, agent = await _browser_harness(make_harness, tmp_path, fake_runtime)
    run_id, hang = await _start_hanging(h, alice, agent, fake_runtime)
    h.app.state.model_gateway.latch.latched = True      # e.g. the breaker, in process
    try:
        row = await _wait_finished(h, str(run_id))
    finally:
        h.app.state.model_gateway.latch.latched = False
    assert (row.status, row.failure_code) == ("failed", "emergency_stop")
    await _assert_torn_down(h, fake_runtime, run_id, hang)


async def test_a_task_closed_by_any_other_path_ends_the_run(make_harness, tmp_path, fake_runtime) -> None:
    h, alice, agent = await _browser_harness(make_harness, tmp_path, fake_runtime)
    run_id, hang = await _start_hanging(h, alice, agent, fake_runtime)
    [row] = await h.rows(AgentRunRow, AgentRunRow.run_id == run_id)
    async with h.storage.session() as s:
        await s.execute(update(AgentTask).where(AgentTask.task_id == row.task_id).values(
            status="failed", failure_code="principal_revoked", finished_at=row.started_at))
        await s.commit()
    row = await _wait_finished(h, str(run_id))
    assert (row.status, row.failure_code) == ("failed", "principal_revoked")
    await _assert_torn_down(h, fake_runtime, run_id, hang)


# ── the system ─────────────────────────────────────────────────────────────


async def test_the_deadline_stops_a_run_that_does_not_finish(make_harness, tmp_path, fake_runtime) -> None:
    config = browser_config(tmp_path)
    config["agent"] = {"bounds": {"wall_clock_timeout_seconds": 1.0}}
    h = await make_harness(config=config, agent_tools=True)
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=BROWSER_DRAFT)
    run_id, hang = await _start_hanging(h, alice, agent, fake_runtime)
    row = await _wait_finished(h, str(run_id))
    assert (row.status, row.failure_code) == ("failed", "wall_clock_timeout")
    await _assert_torn_down(h, fake_runtime, run_id, hang)


async def test_a_run_that_hits_its_model_call_ceiling_is_stopped(make_harness, tmp_path, fake_runtime) -> None:
    h, alice, agent = await _browser_harness(make_harness, tmp_path, fake_runtime)

    async def exhaust(spec) -> None:
        async with h.storage.session() as s:
            [row] = await h.rows(AgentRunRow, AgentRunRow.run_id == spec.run_id)
            await s.execute(update(AgentTask).where(AgentTask.task_id == row.task_id).values(model_calls=10_000))
            await s.commit()

    run_id, hang = await _start_hanging(h, alice, agent, fake_runtime, before=exhaust)
    assert h.model.seen == []       # refused before any provider call; the run ended on that refusal
    row = await _wait_finished(h, str(run_id))
    assert (row.status, row.failure_code) == ("failed", "max_model_calls")
    await _assert_torn_down(h, fake_runtime, run_id, hang)


@pytest.mark.parametrize("code,ends", [
    ("budget_exceeded", "budget_exceeded"), ("agent_budget_exhausted", "budget_exceeded"),
    ("max_model_calls", "max_model_calls"), ("rate_limited", None), ("dependency_unavailable", None),
    ("model_not_allowed", None), ("schema_invalid", None), ("invalid_run_token", None),
])
def test_which_gateway_refusals_end_the_run(code, ends) -> None:
    from server.gateway.model_gateway_port import ModelGatewayReply

    assert run_ending_refusal(ModelGatewayReply(status=429, payload={"error": {"code": code}})) == ends
    assert run_ending_refusal(ModelGatewayReply(status=200, payload={"choices": []})) is None


async def test_a_token_revoked_elsewhere_ends_the_run(make_harness, tmp_path, fake_runtime) -> None:
    h, alice, agent = await _browser_harness(make_harness, tmp_path, fake_runtime)
    run_id, hang = await _start_hanging(h, alice, agent, fake_runtime)
    async with h.storage.session() as s:
        await h.app.state.agent_factory.factory.gateway.revoke_run(s, run_id, "operator_revoked")
        await s.commit()
    row = await _wait_finished(h, str(run_id))
    assert (row.status, row.failure_code) == ("failed", "token_revoked")
    await _assert_torn_down(h, fake_runtime, run_id, hang)


async def test_a_container_the_engine_loses_track_of_is_stopped_and_failed(make_harness, tmp_path,
                                                                         fake_runtime) -> None:
    h, alice, agent = await _browser_harness(make_harness, tmp_path, fake_runtime)
    run_id, hang = await _start_hanging(h, alice, agent, fake_runtime)
    original = fake_runtime.wait

    async def broken(name, *, timeout):
        raise RuntimeError("podman went away")

    fake_runtime.wait = broken
    row = await _wait_finished(h, str(run_id))
    fake_runtime.wait = original
    assert (row.status, row.failure_code) == ("failed", "runtime_crashed")
    await _assert_torn_down(h, fake_runtime, run_id, hang)


async def test_the_runtimes_own_failure_words_never_become_jarviss(make_harness, tmp_path, fake_runtime) -> None:
    h, alice, agent = await _browser_harness(make_harness, tmp_path, fake_runtime)
    await h.grant(alice, BROWSER_CAPABILITY)

    async def lies(spec) -> int:
        fake_runtime.write_result(spec, {"status": "failed", "final": None, "steps": 1, "error": "emergency_stop"})
        return 1

    fake_runtime.behaviour = lies
    resp = await run_agent(h, alice, agent["agent_id"])
    row = await _wait_finished(h, resp.json()["run_id"])
    assert (row.status, row.failure_code) == ("failed", "runtime_failed")


# ── a restart ──────────────────────────────────────────────────────────────


async def test_after_a_restart_an_unfinished_run_is_closed_and_its_container_removed(make_harness, tmp_path,
                                                                                    fake_runtime) -> None:
    h, alice, agent = await _browser_harness(make_harness, tmp_path, fake_runtime)
    run_id, hang = await _start_hanging(h, alice, agent, fake_runtime)
    runs = h.app.state.agent_factory.factory.browser_runs
    # The process dies: nothing of the run in memory survives, nothing is
    # finished, the container is still there.
    live = runs.live.pop(run_id)
    live.supervisor.cancel()
    await asyncio.gather(live.supervisor, return_exceptions=True)
    fake_runtime.stopped.clear()
    fake_runtime.orphans = [f"jarvis-run-{run_id}"]

    assert await runs.recover() == [run_id]
    [row] = await h.rows(AgentRunRow, AgentRunRow.run_id == run_id)
    assert (row.status, row.failure_code) == ("failed", "interrupted")
    assert all(t.revoked_at is not None for t in await h.rows(AgentRunTokenRow, AgentRunTokenRow.run_id == run_id))
    [task] = await h.rows(AgentTask, AgentTask.task_id == row.task_id)
    assert task.status == "failed" and task.finished_at is not None
    active = [g for g in await h.rows(CapabilityGrant, CapabilityGrant.principal_id == row.task_id)
              if g.revoked_at is None]
    assert active == []
    [spec] = fake_runtime.started
    assert not Path(spec.scratch_dir).exists()
    assert any(e.action == "agent.run.finished" and "interrupted" in e.resource for e in await h.rows(AuditEvent))
    # ... and the reconciler removes the container the dead process left.
    reconciler = ContainerReconciler(engine=fake_runtime, storage=h.storage, interval_seconds=600)
    assert await reconciler.reconcile() == [f"jarvis-run-{run_id}"]


# ── attribution and metering ───────────────────────────────────────────────


async def test_the_runs_model_calls_and_egress_are_metered_to_the_owner_and_attributed(make_harness, tmp_path,
                                                                                      fake_runtime) -> None:
    h, alice, agent = await _browser_harness(make_harness, tmp_path, fake_runtime)
    await h.grant(alice, BROWSER_CAPABILITY)
    h.model.push("Nothing new.")
    resp = await run_agent(h, alice, agent["agent_id"])
    row = await _wait_finished(h, resp.json()["run_id"])
    assert row.status == "completed"
    usage = {u.usage_id: u for u in await h.rows(UsageEvent)}
    attributed = {a.usage_id for a in await h.rows(AgentRunUsageRow, AgentRunUsageRow.run_id == row.run_id)}
    kinds = sorted((usage[i].kind, usage[i].tool_id) for i in attributed)
    assert kinds == [("model_call", None), ("tool_call", "browser.session")]
    assert all(usage[i].user_id == alice.user_id for i in attributed)
    [summary] = [e for e in await h.rows(AuditEvent) if e.action == "agent.egress.summary"]
    assert summary.user_id == alice.user_id and str(row.run_id) in summary.resource
    assert await h.rows(AgentInboxItemRow, AgentInboxItemRow.run_id == row.run_id)


# ── never unattended ───────────────────────────────────────────────────────


async def test_a_browser_agent_is_never_unattended(make_harness, tmp_path, fake_runtime) -> None:
    config = browser_config(tmp_path)
    config["agents"].update({k: v for k, v in UNATTENDED_ON["agents"].items()
                             if k in ("unattended_enabled", "standing_delegation_ratified",
                                      "default_budget_per_run", "default_budget_per_month")})
    h = await make_harness(config=config, agent_tools=True)
    alice = await h.user("alice")
    unattended = {**BROWSER_DRAFT, "trigger_request": {"kind": "unattended", "cron": "0 7 * * *",
                                                       "timezone": "Asia/Kolkata"}}
    compiled = await h.client.post(f"{API}/compile", json=unattended, headers=alice.auth)
    # Only on-demand: no template that browses is ever selected for it.
    assert compiled.json()["kind"] in ("rejected", "needs_clarification")
    assert compiled.json()["spec_preview"] is None
    agent = await create_agent(h, alice, draft=BROWSER_DRAFT)
    refused = await request_delegation(h, alice, agent["agent_id"])
    assert refused.status_code in (403, 409, 422), refused.text
    assert "confirmation_token" not in json.dumps(refused.json())
