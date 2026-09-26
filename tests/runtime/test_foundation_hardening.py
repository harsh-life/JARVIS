"""Integration hardening through U6 — regressions for what the audit found.

Each test reproduces a defect found by driving the real application, and
pins the fix.

| Finding | Test |
|---|---|
| hostile JSON from a model crashed the request (no task row, no audit, live state leaked) | `test_hostile_model_output_is_a_rejected_proposal_not_a_crash` |
| any unexpected error did the same | `test_an_unexpected_error_fails_the_task_closed_and_audited` |
| a stop landing during the final model call still ended the task "completed" | `test_a_stop_during_the_last_model_call_is_an_emergency_stop`, `test_a_cancel_during_the_last_model_call_cancels` |
| a duplicate approval rewrote a completed task as failed ("not performed") | `test_a_duplicate_approval_never_rewrites_the_outcome` |
| an approval the store could not take lost the paused action | `test_an_approval_the_store_cannot_take_is_kept_for_a_retry` |
| a busy store surfaced as a raw 500 | `test_a_busy_store_is_a_clean_retryable_503`, `test_a_concurrent_submission_while_the_store_is_held_is_a_clean_503` |
| a restart left interrupted tasks non-terminal with live task grants | `test_a_restart_closes_the_tasks_it_interrupted`, `test_startup_runs_the_reconciliation` |
| closing a task from its row wrote no audit and could overwrite a finished task | `test_closing_from_the_row_is_audited_and_conditional` |
| stop matrix: worker switch, tool call | `test_a_stop_during_a_worker_switch_runs_nothing_more`, `test_a_stop_during_a_tool_call_aborts_it` |
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid

import pytest
from sqlalchemy.exc import OperationalError

from server.agent import runtime as runtime_module
from server.agent.records import close_if_live
from server.models.provider import ModelUnavailable
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.storage.models import AgentTask, AuditEvent, CapabilityGrant, ConfirmationToken, UsageEvent
from shared.schemas.agent import AgentTaskStatus
from shared.schemas.enums import AuditActor
from tests.runtime.conftest import (
    ScriptedModel,
    ask,
    call,
    failure_of,
    final,
    make_harness,  # noqa: F401 — fixture
    pending_of,
)

pytestmark = pytest.mark.asyncio

PKG = {"package_name": "com.example"}


def ui(operation: str, **args) -> str:
    return call("ui.app", operation, args=args or {"id": "x"}, platform="android")


async def actions(h) -> list[str]:
    return [r.action for r in await h.rows(AuditEvent)]


def only_live_state(h):
    [state] = list(h.runtime.states.live())
    return state


def locked() -> OperationalError:
    return OperationalError("UPDATE agent_tasks", {}, sqlite3.OperationalError("database is locked"))


# ── untrusted model output never crashes the task ──────────────────────────


@pytest.mark.parametrize("hostile", [
    '{"type":"tool_call","tool":"t","operation":"o","arguments":' + '{"a":' * 3000 + '1' + '}' * 3000 + '}',
    '{"type":"tool_call","tool":"t","operation":"o","arguments":{"n":' + '9' * 5000 + '}}',
    '{"type":"tool_call","tool":"t","operation":"o","arguments":' + '[' * 950 + ']' * 950 + '}',
])
async def test_hostile_model_output_is_a_rejected_proposal_not_a_crash(h, hostile):
    alice = await h.user("alice")
    h.model.push(hostile, final("recovered"))

    resp = await h.submit(alice)

    assert resp.status_code == 200, resp.text
    assert resp.json()["response"] == "recovered"
    assert "not a valid proposal" in h.model.all_text()
    [row] = await h.rows(AgentTask)
    assert row.status == "completed"
    assert AuditAction.AGENT_PROPOSAL_REJECTED.value in await actions(h)
    assert list(h.runtime.states.live()) == []


async def test_an_unexpected_error_fails_the_task_closed_and_audited(h):
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)

    def boom(messages):
        raise RuntimeError("a dependency failed in a way nothing anticipated")

    h.model.push(ask("app.interact", scope=PKG), ui("read_screen_element"), boom)
    resp = await h.submit(alice)

    assert resp.status_code == 500, resp.text
    assert failure_of(resp) == "internal_error"
    assert "anticipated" not in resp.text  # no internal detail reaches the client
    [row] = await h.rows(AgentTask)
    assert (row.status, row.failure_code) == ("failed", "internal_error")
    seen = await actions(h)
    assert AuditAction.AGENT_TASK_SUBMITTED.value in seen and AuditAction.AGENT_TASK_FAILED.value in seen
    assert list(h.runtime.states.live()) == []
    assert len(await h.rows(UsageEvent)) >= 2  # the metered calls survived the failure


async def test_an_unexpected_error_during_an_approved_action_fails_closed(h):
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    h.model.push(ask("app.interact", scope=PKG), ui("input_text", text="hi"))
    paused = pending_of(await h.submit(alice))

    def boom(messages):
        raise RuntimeError("boom")

    h.model.push(boom)
    resp = await h.confirm(alice, paused["task_id"], paused["confirmation_token"])

    assert resp.status_code == 500 and failure_of(resp) == "internal_error"
    assert len(h.ui.calls) == 1  # the approved action ran once, before the failure
    [row] = await h.rows(AgentTask)
    assert (row.status, row.failure_code) == ("failed", "internal_error")
    assert list(h.runtime.states.live()) == []


# ── a stop is never out-raced by the model's last answer ──────────────────


async def test_a_stop_during_the_last_model_call_is_an_emergency_stop(h):
    alice = await h.user("alice")

    def answers_as_it_is_stopped(messages):
        h.runtime.signal_stop(only_live_state(h).task_id, reason="runaway", source="operator")
        return final("done anyway")

    h.model.push(answers_as_it_is_stopped)
    resp = await h.submit(alice)

    assert resp.status_code == 409 and failure_of(resp) == "emergency_stop"
    [row] = await h.rows(AgentTask)
    assert (row.status, row.failure_code, row.response) == ("failed", "emergency_stop", None)


async def test_a_cancel_during_the_last_model_call_cancels(h):
    alice = await h.user("alice")

    def answers_as_it_is_cancelled(messages):
        state = only_live_state(h)
        state.cancelled = True
        state.cancel_event.set()
        return final("done anyway")

    h.model.push(answers_as_it_is_cancelled)
    resp = await h.submit(alice)

    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
    assert resp.json()["response"] is None


# ── approvals: duplicates and a busy store ─────────────────────────────────


async def test_a_duplicate_approval_never_rewrites_the_outcome(h):
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    h.model.push(ask("app.interact", scope=PKG), ui("input_text", text="hi"))
    paused = pending_of(await h.submit(alice))
    running = asyncio.Event()

    async def slow(messages):
        running.set()
        await asyncio.sleep(0.5)
        return final("done")

    h.model.push(slow)
    first = asyncio.create_task(h.confirm(alice, paused["task_id"], paused["confirmation_token"]))
    await running.wait()
    second = await h.confirm(alice, paused["task_id"], paused["confirmation_token"])
    first = await first

    assert first.status_code == 200 and first.json()["status"] == "completed"
    assert second.status_code == 409 and "not awaiting" in second.json()["error"]["message"]
    assert len(h.ui.calls) == 1
    [row] = await h.rows(AgentTask)
    assert (row.status, row.failure_code) == ("completed", None)
    assert AuditAction.AGENT_TASK_ABANDONED.value not in await actions(h)


async def test_an_approval_the_store_cannot_take_is_kept_for_a_retry(h, monkeypatch):
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    h.model.push(ask("app.interact", scope=PKG), ui("input_text", text="hi"))
    paused = pending_of(await h.submit(alice))
    real = runtime_module.update_task_row
    failures = {"left": 1}

    async def busy_once(session, task_id, **kwargs):
        if kwargs.get("status") is AgentTaskStatus.RUNNING and failures["left"]:
            failures["left"] -= 1
            raise locked()
        return await real(session, task_id, **kwargs)

    monkeypatch.setattr(runtime_module, "update_task_row", busy_once)
    busy = await h.confirm(alice, paused["task_id"], paused["confirmation_token"])

    assert busy.status_code == 503, busy.text
    assert busy.json()["error"]["details"] == {"dependency": "storage"}
    assert busy.json()["error"]["retryable"] is True
    assert h.ui.calls == []  # nothing ran

    h.model.push(final("done"))
    retried = await h.confirm(alice, paused["task_id"], paused["confirmation_token"])
    assert retried.status_code == 200 and retried.json()["status"] == "completed"
    assert len(h.ui.calls) == 1


async def test_a_busy_store_is_a_clean_retryable_503_and_other_errors_stay_500(h, monkeypatch):
    alice = await h.user("alice")

    async def busy(*args, **kwargs):
        raise locked()

    monkeypatch.setattr(runtime_module, "create_task_row", busy)
    resp = await h.submit(alice)
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "dependency_unavailable"
    assert await h.rows(AgentTask) == [] and list(h.runtime.states.live()) == []

    async def broken(*args, **kwargs):
        raise OperationalError("INSERT", {}, sqlite3.OperationalError("disk I/O error"))

    monkeypatch.setattr(runtime_module, "create_task_row", broken)
    other = await h.submit(alice)
    assert other.status_code == 500 and other.json()["error"]["code"] == "internal_error"
    assert "disk" not in other.text


async def test_a_concurrent_submission_while_the_store_is_held_is_a_clean_503(h):
    """SQLite is single-writer and a task's request holds the write lock until
    it ends. A second request that needs to write waits the driver's busy
    timeout (5 s) and then gets a clean, retryable 503 — with nothing of it
    persisted — instead of a raw 500. The limitation itself is documented,
    not redesigned (docs/RUNNING_RUNTIME.md)."""

    alice, bob = await h.user("alice"), await h.user("bob")
    holding = asyncio.Event()

    async def slow(messages):
        holding.set()
        await asyncio.sleep(6.5)
        return final("slow done")

    h.model.push(slow)
    first = asyncio.create_task(h.submit(alice, "one"))
    await holding.wait()
    started = time.monotonic()
    second = await h.submit(bob, "two")

    assert second.status_code == 503, second.text
    assert second.json()["error"]["details"] == {"dependency": "storage"}
    assert time.monotonic() - started < 6.5  # it failed fast-ish, it did not wait the task out
    assert (await first).status_code == 200
    rows = await h.rows(AgentTask)
    assert [r.user_id for r in rows] == [alice.user_id]
    assert [s.principal.user_id for s in h.runtime.states.live()] == []


# ── a restart fails what it interrupted closed ────────────────────────────


async def _reconcile(h) -> list[uuid.UUID]:
    async with h.storage.session() as s:
        closed = await h.app.state.agent_tasks.reconcile_after_restart(
            s, audit=AuditLogger(s, request_id=uuid.uuid4()))
        await s.commit()
    return closed


async def test_a_restart_closes_the_tasks_it_interrupted(h):
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    h.model.push(ask("app.interact", scope=PKG), ui("input_text", text="hi"))
    paused = pending_of(await h.submit(alice))
    h.model.push(final("done"))
    done = (await h.submit(alice)).json()
    h.model.push(ask("app.interact", scope=PKG), ui("input_text", text="again"))
    live = pending_of(await h.submit(alice))

    # The "restart": the paused task's state is gone; another row was left
    # mid-run by the crash; the third task is still live in this process.
    h.runtime.states.pop(uuid.UUID(paused["task_id"]))
    crashed = uuid.uuid4()
    async with h.storage.session() as s:
        row = await s.get(AgentTask, uuid.UUID(done["task_id"]))
        s.add(AgentTask(task_id=crashed, user_id=row.user_id, device_id=row.device_id,
                        session_id=row.session_id, graph_id=None, status="running", mode="execute",
                        iterations=1, worker_switches=0, model_calls=1, tool_calls=0,
                        created_at=row.created_at, updated_at=row.updated_at))
        await s.commit()

    closed = await _reconcile(h)

    assert set(closed) == {uuid.UUID(paused["task_id"]), crashed}
    by_id = {r.task_id: r for r in await h.rows(AgentTask)}
    assert (by_id[uuid.UUID(paused["task_id"])].status,
            by_id[uuid.UUID(paused["task_id"])].failure_code) == ("failed", "confirmation_state_lost")
    assert (by_id[crashed].status, by_id[crashed].failure_code) == ("failed", "internal_error")
    assert by_id[uuid.UUID(done["task_id"])].status == "completed"                     # untouched
    assert by_id[uuid.UUID(live["task_id"])].status == "awaiting_confirmation"        # live: untouched

    tokens = {t.task_id: t for t in await h.rows(ConfirmationToken)}
    assert tokens[paused["task_id"]].used_at is not None          # spent
    assert tokens[live["task_id"]].used_at is None                # the live one still usable
    task_grants = [g for g in await h.rows(CapabilityGrant) if g.scope_type == "task"]
    assert all((g.revoked_at is not None) == (g.scope_id == paused["task_id"]) for g in task_grants)
    abandoned = [r for r in await h.rows(AuditEvent) if r.action == AuditAction.AGENT_TASK_ABANDONED.value]
    assert {r.resource for r in abandoned} == {
        f"agent_task:{paused['task_id']}:confirmation_state_lost", f"agent_task:{crashed}:internal_error"}
    assert all(r.actor == AuditActor.SYSTEM.value for r in abandoned)

    assert await _reconcile(h) == []  # idempotent
    stale = await h.confirm(alice, paused["task_id"], paused["confirmation_token"])
    assert stale.status_code == 409


async def test_startup_runs_the_reconciliation(tmp_path):
    from server.gateway.app import create_app
    from server.storage import SQLAlchemyStorageBackend
    from tests.support import make_test_config

    class Port:
        calls = 0

        async def reconcile_after_restart(self, session, *, audit):
            Port.calls += 1
            return [uuid.uuid4()]

    storage = SQLAlchemyStorageBackend(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
    await storage.init_models()
    for flag, expected in ((False, 0), (True, 1)):
        Port.calls = 0
        app = create_app(config=make_test_config(), storage=SQLAlchemyStorageBackend(
            f"sqlite+aiosqlite:///{tmp_path / 'r.db'}"), agent_tasks=Port(), reconcile_tasks_on_startup=flag)
        async with app.router.lifespan_context(app):
            pass
        assert Port.calls == expected
    await storage.dispose()


# ── closing a task from its row ────────────────────────────────────────────


async def test_closing_from_the_row_is_audited_and_conditional(h):
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    h.model.push(ask("app.interact", scope=PKG), ui("input_text", text="hi"))
    paused = pending_of(await h.submit(alice))
    h.runtime.states.pop(uuid.UUID(paused["task_id"]))

    resp = await h.client.post(f"/api/v1/agent/tasks/{paused['task_id']}/cancel", headers=alice.auth)
    assert resp.json()["status"] == "cancelled"
    cancelled = [r for r in await h.rows(AuditEvent) if r.action == AuditAction.AGENT_TASK_CANCELLED.value]
    assert [r.resource for r in cancelled] == [f"agent_task:{paused['task_id']}"]

    # A finished task is never rewritten by a close that read a stale row.
    h.model.push(final("done"))
    done = (await h.submit(alice)).json()
    async with h.storage.session() as s:
        row, closed = await close_if_live(s, uuid.UUID(done["task_id"]), status=AgentTaskStatus.FAILED,
                                          failure_code="confirmation_state_lost")
        await s.commit()
    assert closed is False and (row.status, row.failure_code) == ("completed", None)


# ── the stop matrix: the cells not covered elsewhere ──────────────────────


async def test_a_stop_during_a_worker_switch_runs_nothing_more(make_harness):
    w2 = ScriptedModel("w2")
    h = await make_harness(config={"agent": {"recovery": {"chain": [{"provider": "ollama", "model": "w2"}]}}},
                           models={"w2": w2})
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)

    def down_as_it_is_stopped(messages):
        h.runtime.signal_stop(only_live_state(h).task_id, reason="runaway", source="operator")
        raise ModelUnavailable("primary down")

    h.model.push(ask("app.interact", scope=PKG), down_as_it_is_stopped)
    w2.push(ui("read_screen_element"), final("w2 answered"))
    resp = await h.submit(alice)

    assert resp.status_code == 409 and failure_of(resp) == "emergency_stop"
    assert w2.seen == [] and h.ui.calls == []  # the replacement was never even asked
    assert AuditAction.AGENT_WORKER_SWITCHED.value in await actions(h)


async def test_a_stop_during_a_tool_call_aborts_it(h):
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    in_tool = asyncio.Event()
    finished = []

    async def slow_execute(invocation):
        in_tool.set()
        await asyncio.sleep(30)
        finished.append(invocation)

    h.ui.execute = slow_execute
    h.model.push(ask("app.interact", scope=PKG), ui("read_screen_element"), final("unreachable"))
    started = time.monotonic()
    running = asyncio.create_task(h.submit(alice))
    await in_tool.wait()
    h.runtime.signal_stop(only_live_state(h).task_id, reason="runaway", source="operator")
    resp = await asyncio.wait_for(running, timeout=10)

    assert failure_of(resp) == "emergency_stop"
    assert time.monotonic() - started < 10 and finished == []
    failed = [r.resource for r in await h.rows(AuditEvent) if r.action == AuditAction.AGENT_TOOL_FAILED.value]
    assert failed == ["tool:ui.app.read_screen_element:cancelled"]


async def test_global_clear_never_resumes_what_the_stop_ended(h, monkeypatch):
    from server.security.superuser import SUPERUSER_TOKEN_ENV

    token = "TEST-ONLY-superuser-credential-0123456789abcdef"
    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, token)
    su = {"Authorization": f"Superuser {token}"}
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    h.model.push(ask("app.interact", scope=PKG), ui("input_text", text="hi"))
    paused = pending_of(await h.submit(alice))

    assert (await h.client.post("/api/v1/admin/control/global-stop", headers=su,
                                json={"reason": "incident"})).status_code == 200
    assert (await h.client.post("/api/v1/admin/control/global-clear", headers=su, json={})).status_code == 200

    refused = await h.confirm(alice, paused["task_id"], paused["confirmation_token"])
    assert refused.status_code == 409 and h.ui.calls == []
    fetched = (await h.get(alice, paused["task_id"])).json()
    assert (fetched["status"], fetched["failure"]["code"]) == ("failed", "emergency_stop")


# ── the guards, each on its own ────────────────────────────────────────────


async def test_an_error_before_the_first_step_fails_the_task_closed(h, monkeypatch):
    alice = await h.user("alice")

    def boom(*args, **kwargs):
        raise RuntimeError("context assembly failed")

    monkeypatch.setattr(runtime_module.ctx, "context_message", boom)
    resp = await h.submit(alice)

    assert resp.status_code == 500 and failure_of(resp) == "internal_error"
    [row] = await h.rows(AgentTask)
    assert (row.status, row.failure_code) == ("failed", "internal_error")
    assert list(h.runtime.states.live()) == []


async def test_an_error_while_running_an_approved_action_fails_the_task_closed(h, monkeypatch):
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    h.model.push(ask("app.interact", scope=PKG), ui("input_text", text="hi"))
    paused = pending_of(await h.submit(alice))

    def boom(*args, **kwargs):
        raise RuntimeError("observation assembly failed")

    monkeypatch.setattr(runtime_module.ctx, "tool_observation", boom)
    resp = await h.confirm(alice, paused["task_id"], paused["confirmation_token"])

    assert resp.status_code == 500 and failure_of(resp) == "internal_error"
    [row] = await h.rows(AgentTask)
    assert (row.status, row.failure_code) == ("failed", "internal_error")
    assert list(h.runtime.states.live()) == []
    tokens = await h.rows(ConfirmationToken)
    assert tokens and all(t.used_at is not None for t in tokens)


async def test_when_even_the_failure_cannot_be_recorded_no_state_survives(h, monkeypatch):
    alice = await h.user("alice")
    real = runtime_module.update_task_row

    async def store_down_for_endings(session, task_id, **kwargs):
        if kwargs.get("status") is AgentTaskStatus.FAILED:
            raise RuntimeError("store unavailable")
        return await real(session, task_id, **kwargs)

    def boom(messages):
        raise RuntimeError("boom")

    monkeypatch.setattr(runtime_module, "update_task_row", store_down_for_endings)
    h.model.push(boom)
    with pytest.raises(RuntimeError):  # nothing can be recorded; the request fails
        await h.submit(alice)
    assert list(h.runtime.states.live()) == []


async def test_closing_an_already_finished_task_from_its_row_does_nothing(h):
    alice = await h.user("alice")
    h.model.push(final("done"))
    done = (await h.submit(alice)).json()
    before = len(await h.rows(AuditEvent))
    async with h.storage.session() as s:
        row = await s.get(AgentTask, uuid.UUID(done["task_id"]))
        env = h.app.state.agent_tasks.environment(s, AuditLogger(s, request_id=uuid.uuid4()))
        updated, closed = await h.runtime._close_from_row(env, row, AgentTaskStatus.CANCELLED, None,
                                                          event=runtime_module.AgentEvent.TASK_CANCELLED)
        await s.commit()
    assert closed is False and updated.status == "completed"
    assert len(await h.rows(AuditEvent)) == before  # no cleanup, no audit: it already ended
