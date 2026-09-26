"""Operator stop and the global emergency latch — build unit U3
(`docs/18_SUPERVISORY_RUNTIME_RECOVERY_CONTROL.md` §5.4).

Everything runs through the real application: real superuser authentication
(`Authorization: Superuser <token>`), the real runtime and breaker, real
confirmation tokens, and the real SQLite store — including a stop issued while
another request is in the middle of a model call and holds the write lock.

| Requirement | Test |
|---|---|
| valid superuser stops a task | `test_a_superuser_stops_a_paused_task`, `test_a_superuser_stops_a_running_task_mid_model_call` |
| invalid credential cannot stop | `test_a_wrong_credential_cannot_stop_or_latch` |
| ordinary Bearer cannot stop | `test_an_ordinary_bearer_token_cannot_reach_any_control` |
| latch blocks new submissions | `test_the_global_latch_refuses_new_submissions` |
| latch is no alternate authorization path | `test_the_latch_is_not_an_authorization_path` |
| running tasks are stopped | `test_a_global_stop_stops_running_and_paused_tasks` |
| clear requires superuser | `test_an_ordinary_bearer_token_cannot_reach_any_control`, `test_a_wrong_credential_cannot_stop_or_latch` |
| latch/clear audited; task stop audited | `test_latch_and_clear_are_audited`, `test_a_superuser_stops_a_paused_task` |
| repeated stop / clear idempotent | `test_stopping_twice_is_idempotent`, `test_latching_and_clearing_twice_are_idempotent` |
| latch fails closed | `test_the_latch_survives_a_restart`, `test_an_unreadable_latch_refuses_submissions`, `test_a_task_created_during_a_global_stop_is_stopped` |
| normal user functionality unchanged | `test_ordinary_use_is_unchanged_once_cleared` |
| import boundaries | `test_the_control_modules_are_import_restricted` |
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from server.composition import build_application
from server.composition.latch import SupervisorGate
from server.security.events import AuditAction
from server.security.superuser import SUPERUSER_TOKEN_ENV
from server.storage.models import AgentTask, AuditEvent, ConfirmationToken, SupervisorLatch
from shared.schemas.enums import AuditActor, AuditResult
from tests.runtime.conftest import ask, call, failure_of, final, make_harness, pending_of  # noqa: F401

pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "TEST-ONLY-superuser-credential-0123456789abcdef"
SU = {"Authorization": f"Superuser {TOKEN}"}
CONTROL = "/api/v1/admin/control"
PKG = {"package_name": "com.example"}


@pytest_asyncio.fixture
async def h(make_harness, monkeypatch):
    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, TOKEN)
    return await make_harness()


async def stop(h, target_id, *, scope: str = "task", reason: str = "runaway", headers=SU) -> httpx.Response:
    return await h.client.post(f"{CONTROL}/stop", headers=headers,
                               json={"scope": scope, "target_id": str(target_id), "reason": reason})


async def global_stop(h, reason: str = "incident", headers=SU) -> httpx.Response:
    return await h.client.post(f"{CONTROL}/global-stop", headers=headers, json={"reason": reason})


async def global_clear(h, headers=SU) -> httpx.Response:
    return await h.client.post(f"{CONTROL}/global-clear", headers=headers, json={})


async def paused_task(h, actor, text_: str = "x") -> dict:
    h.model.push(ask("app.interact", scope=PKG),
                 call("ui.app", "input_text", args={"text": text_}, platform="android"))
    return pending_of(await h.submit(actor))


async def audit(h, action: AuditAction) -> list[AuditEvent]:
    return [r for r in await h.rows(AuditEvent) if r.action == action.value]


def stopped_task(resp: httpx.Response) -> str:
    assert resp.status_code == 409, resp.text
    assert failure_of(resp) == "emergency_stop"
    return resp.json()["error"]["message"]


def suspended(resp: httpx.Response) -> None:
    assert resp.status_code == 503, resp.text
    error = resp.json()["error"]
    assert (error["code"], error["details"]["dependency"]) == ("dependency_unavailable", "supervisor")


# ── stopping a task ────────────────────────────────────────────────────────


async def test_a_superuser_stops_a_paused_task(h):
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    paused = await paused_task(h, alice, "pay now")

    resp = await stop(h, paused["task_id"])

    assert resp.status_code == 200, resp.text
    assert resp.json()["tasks"] == {"stopped": [paused["task_id"]], "signalled": [], "already_terminal": []}
    # The owner's valid, unexpired token now approves nothing: the task is terminal.
    refused = await h.confirm(alice, paused["task_id"], paused["confirmation_token"])
    assert refused.status_code == 409, refused.text
    fetched = (await h.get(alice, paused["task_id"])).json()
    assert (fetched["status"], fetched["failure"]["code"]) == ("failed", "emergency_stop")
    assert h.ui.calls == []
    assert all(t.used_at is not None for t in await h.rows(ConfirmationToken))
    [row] = await h.rows(AgentTask)
    assert (row.status, row.failure_code) == ("failed", "emergency_stop")

    [tripped] = await audit(h, AuditAction.BREAKER_TRIPPED)
    assert tripped.resource == f"breaker:task:{paused['task_id']}:operator:runaway"
    assert tripped.user_id == alice.user_id  # attributed to the task's owner, enforced by the system
    [control] = await audit(h, AuditAction.CONTROL_STOP)
    assert (control.actor, control.result) == (AuditActor.SUPERUSER.value, AuditResult.SUCCESS.value)
    assert control.resource == f"control:stop:task:{paused['task_id']}:runaway"
    [auth] = await audit(h, AuditAction.SUPERUSER_AUTHENTICATED)
    assert auth.request_id == control.request_id
    assert all(TOKEN not in r.resource for r in await h.rows(AuditEvent))


async def test_a_superuser_stops_a_running_task_mid_model_call(h):
    """The task's own request is in the middle of a model call and holds the
    store's write lock. The stop trips it in memory first, the model call is
    abandoned, the task stops and commits, and only then does the operator's
    request write — so neither waits on the other."""

    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    in_model_call = asyncio.Event()

    async def slow_model(messages):
        in_model_call.set()
        await asyncio.Event().wait()  # never returns on its own
        return call("ui.app", "input_text", args={"text": "x"}, platform="android")

    h.model.push(ask("app.interact", scope=PKG), slow_model, final("never reached"))
    submitted = asyncio.create_task(h.submit(alice))
    await asyncio.wait_for(in_model_call.wait(), timeout=5)
    [live] = h.runtime.states.live()

    resp = await asyncio.wait_for(stop(h, live.task_id), timeout=10)
    task_resp = await asyncio.wait_for(submitted, timeout=10)

    assert resp.status_code == 200, resp.text
    assert resp.json()["tasks"]["signalled"] == [str(live.task_id)]
    assert "server operator" in stopped_task(task_resp)
    assert h.ui.calls == []
    assert len(h.model.script) == 1
    [tripped] = await audit(h, AuditAction.BREAKER_TRIPPED)
    assert tripped.resource == f"breaker:task:{live.task_id}:operator:runaway"


async def test_stopping_twice_is_idempotent(h):
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    paused = await paused_task(h, alice)

    first, second = await stop(h, paused["task_id"]), await stop(h, paused["task_id"])

    assert first.json()["tasks"]["stopped"] == [paused["task_id"]]
    assert second.status_code == 200
    assert second.json()["tasks"] == {"stopped": [], "signalled": [], "already_terminal": [paused["task_id"]]}
    assert len(await audit(h, AuditAction.BREAKER_TRIPPED)) == 1
    assert len(await audit(h, AuditAction.CONTROL_STOP)) == 2  # every privileged call is recorded


async def test_a_stop_already_being_enforced_is_not_enforced_twice(h):
    """An operator stop and the owner's /confirm can race to enforce the same
    trip. Whichever arrives second only reports it: one `breaker.tripped`, one
    terminal transition — here, the second arrival writes nothing at all."""

    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    paused = await paused_task(h, alice)
    task_id = uuid.UUID(paused["task_id"])
    h.runtime.signal_stop(task_id, reason="runaway", source="operator")
    h.runtime.states.get(task_id).stop_enforced = True  # "another request is enforcing it"

    resp = await h.confirm(alice, paused["task_id"], paused["confirmation_token"])

    assert "server operator" in stopped_task(resp)
    assert await audit(h, AuditAction.BREAKER_TRIPPED) == []  # the enforcer writes it, not this call
    [row] = await h.rows(AgentTask)
    assert row.status == "awaiting_confirmation"  # this call did not transition the task
    assert h.ui.calls == []


async def test_stopping_an_unknown_task_is_404_and_audited(h):
    resp = await stop(h, uuid.uuid4())
    assert resp.status_code == 404
    [control] = await audit(h, AuditAction.CONTROL_STOP)
    assert control.result == AuditResult.FAILURE.value


async def test_user_and_device_scopes_stop_only_their_own_tasks(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    for actor in (alice, bob):
        await h.grant(actor, "app.interact", resource_scope=PKG)
    a1, b1 = await paused_task(h, alice), await paused_task(h, bob)

    resp = await stop(h, alice.user_id, scope="user")
    assert resp.json()["tasks"]["stopped"] == [a1["task_id"]]

    # Bob's task is untouched and still confirmable.
    ok = await h.confirm(bob, b1["task_id"], b1["confirmation_token"])
    assert ok.status_code == 200, ok.text
    assert [c.user_id for c in h.ui.calls] == [bob.user_id]

    b2 = await paused_task(h, bob)
    resp = await stop(h, bob.device_id, scope="device")
    assert resp.json()["tasks"]["stopped"] == [b2["task_id"]]


async def test_a_task_with_no_live_state_is_closed_from_its_row(h):
    """After a restart a paused task has a row but no in-memory state; an
    operator stop still closes it — status, grants and tokens."""

    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    paused = await paused_task(h, alice)
    h.runtime.states.pop(uuid.UUID(paused["task_id"]))

    resp = await stop(h, paused["task_id"])

    assert resp.json()["tasks"]["stopped"] == [paused["task_id"]]
    [row] = await h.rows(AgentTask)
    assert (row.status, row.failure_code) == ("failed", "emergency_stop")
    assert all(t.used_at is not None for t in await h.rows(ConfirmationToken))
    assert len(await audit(h, AuditAction.BREAKER_TRIPPED)) == 1


# ── who may use the controls ───────────────────────────────────────────────


async def test_an_ordinary_bearer_token_cannot_reach_any_control(h):
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    paused = await paused_task(h, alice)
    assert (await global_stop(h)).status_code == 200  # latch, to test clear below

    for headers in (alice.auth, {"Authorization": f"Bearer {TOKEN}"}, {}):
        assert (await stop(h, paused["task_id"], headers=headers)).status_code == 401
        assert (await stop(h, alice.user_id, scope="user", headers=headers)).status_code == 401
        assert (await global_stop(h, headers=headers)).status_code == 401
        assert (await global_clear(h, headers=headers)).status_code == 401

    suspended(await h.submit(alice))  # the latch is still set
    assert len(await audit(h, AuditAction.CONTROL_GLOBAL_CLEAR)) == 0


async def test_a_wrong_credential_cannot_stop_or_latch(h):
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    paused = await paused_task(h, alice)
    wrong = {"Authorization": f"Superuser {TOKEN[:-1]}X"}

    assert (await stop(h, paused["task_id"], headers=wrong)).status_code == 401
    assert (await global_stop(h, headers=wrong)).status_code == 401
    assert (await global_clear(h, headers=wrong)).status_code == 401
    # Authentication comes first: an unauthenticated caller learns nothing
    # from body validation either.
    bad_body = await h.client.post(f"{CONTROL}/stop", headers=wrong, json={"reason": "Prose reason!"})
    assert bad_body.status_code == 401

    # Nothing changed: the task is still paused and confirmable, no latch.
    assert h.runtime.states.get(uuid.UUID(paused["task_id"])).tripped is None
    ok = await h.confirm(alice, paused["task_id"], paused["confirmation_token"])
    assert ok.status_code == 200, ok.text
    assert (await h.rows(SupervisorLatch)) == []
    assert len(await audit(h, AuditAction.SUPERUSER_REJECTED)) == 4
    assert await audit(h, AuditAction.CONTROL_STOP) == []


async def test_reasons_are_identifiers(h):
    for reason in ("", "Has Spaces", "x" * 40, "token=sk-abc"):
        assert (await stop(h, uuid.uuid4(), reason=reason)).status_code == 422
        assert (await global_stop(h, reason=reason)).status_code == 422
    unknown_field = await h.client.post(f"{CONTROL}/global-stop", headers=SU,
                                        json={"reason": "ok", "resume": True})
    assert unknown_field.status_code == 422


# ── the global latch ───────────────────────────────────────────────────────


async def test_the_global_latch_refuses_new_submissions(h):
    alice = await h.user("alice")
    latched = await global_stop(h)
    assert latched.status_code == 200 and latched.json()["latched"] is True

    h.model.push(final("never"))
    suspended(await h.submit(alice))
    assert await h.rows(AgentTask) == []  # no task was created at all
    assert h.model.seen == []  # and no model was called

    cleared = await global_clear(h)
    assert cleared.json() == {"latched": False, "changed": True,
                              "tasks": {"stopped": [], "signalled": [], "already_terminal": []}}
    resp = await h.submit(alice)
    assert resp.status_code == 200 and resp.json()["status"] == "completed"


async def test_a_global_stop_stops_running_and_paused_tasks(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    for actor in (alice, bob):
        await h.grant(actor, "app.interact", resource_scope=PKG)
    paused = await paused_task(h, bob)

    in_model_call = asyncio.Event()

    async def slow_model(messages):
        in_model_call.set()
        await asyncio.Event().wait()
        return final("never")

    h.model.push(slow_model)
    running = asyncio.create_task(h.submit(alice))
    await asyncio.wait_for(in_model_call.wait(), timeout=5)
    running_id = next(s.task_id for s in h.runtime.states.live() if s.principal.user_id == alice.user_id)

    resp = await asyncio.wait_for(global_stop(h), timeout=10)
    running_resp = await asyncio.wait_for(running, timeout=10)

    tasks = resp.json()["tasks"]
    assert tasks["signalled"] == [str(running_id)]
    assert tasks["stopped"] == [paused["task_id"]]
    assert "suspended all tasks" in stopped_task(running_resp)
    refused = await h.confirm(bob, paused["task_id"], paused["confirmation_token"])
    assert refused.status_code == 409, refused.text
    assert h.ui.calls == []
    assert {r.resource.split(":")[3] for r in await audit(h, AuditAction.BREAKER_TRIPPED)} == {"global_latch"}


async def test_the_latch_is_not_an_authorization_path(h):
    """Neither the latch nor its clearing grants, resumes or authorizes
    anything. The superuser credential is not a user identity; clearing does
    not restart what was stopped; and afterwards every ordinary rule applies
    exactly as before."""

    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    paused = await paused_task(h, alice)
    await global_stop(h)

    # The operator cannot submit a task: the control credential is not a user.
    for scheme in ("Superuser", "Bearer"):
        resp = await h.client.post("/api/v1/agent/tasks", json={"input": "x"},
                                   headers={"Authorization": f"{scheme} {TOKEN}", "Idempotency-Key": "k"})
        assert resp.status_code == 401

    await global_clear(h)
    # Clearing did not resume the stopped task.
    again = await h.confirm(alice, paused["task_id"], paused["confirmation_token"])
    assert again.status_code == 409
    assert h.ui.calls == []

    # A consequential action still needs its confirmation...
    fresh = await paused_task(h, alice)
    assert fresh["pending"]["risk_category"] == "consequential"
    # ...and a capability the user never granted still needs the human.
    h.model.push(ask("file.read"))
    activation = pending_of(await h.submit(alice))
    assert activation["pending"]["kind"] == "capability_activation"
    assert h.reads.calls == []


async def test_latch_and_clear_are_audited(h):
    await global_stop(h, reason="incident")
    await global_clear(h)

    [latched] = await audit(h, AuditAction.BREAKER_GLOBAL_LATCHED)
    [cleared] = await audit(h, AuditAction.BREAKER_GLOBAL_CLEARED)
    [stop_call] = await audit(h, AuditAction.CONTROL_GLOBAL_STOP)
    [clear_call] = await audit(h, AuditAction.CONTROL_GLOBAL_CLEAR)
    assert latched.resource == "breaker:global:incident"
    assert cleared.resource == "breaker:global"
    assert stop_call.resource == "control:global_stop:incident:latched"
    assert clear_call.resource == "control:global_clear:cleared"
    for row in (latched, cleared, stop_call, clear_call):
        assert (row.actor, row.result) == (AuditActor.SUPERUSER.value, AuditResult.SUCCESS.value)
    [latch_row] = await h.rows(SupervisorLatch)
    assert latch_row.latched is False and latch_row.changed_by and TOKEN not in latch_row.changed_by


async def test_latching_and_clearing_twice_are_idempotent(h):
    assert (await global_clear(h)).json()["changed"] is False  # clear when already clear
    assert (await global_stop(h)).json()["changed"] is True
    assert (await global_stop(h)).json()["changed"] is False
    assert (await global_clear(h)).json()["changed"] is True
    assert (await global_clear(h)).json()["changed"] is False

    assert len(await audit(h, AuditAction.BREAKER_GLOBAL_LATCHED)) == 1   # transitions only
    assert len(await audit(h, AuditAction.BREAKER_GLOBAL_CLEARED)) == 1
    assert [r.resource for r in await audit(h, AuditAction.CONTROL_GLOBAL_CLEAR)] == [
        "control:global_clear:already_clear", "control:global_clear:cleared", "control:global_clear:already_clear",
    ]
    assert [r.resource for r in await audit(h, AuditAction.CONTROL_GLOBAL_STOP)] == [
        "control:global_stop:incident:latched", "control:global_stop:incident:already_latched",
    ]


# ── failing closed ─────────────────────────────────────────────────────────


async def test_the_latch_survives_a_restart(h):
    """A new process (fresh in-memory latch, same database) is still latched:
    only the operator clears it, never a restart."""

    alice = await h.user("alice")
    await global_stop(h)

    restarted = build_application(h.config, storage=h.storage, security=h.core,
                                  provider_factory=lambda spec, key_provider=None: h.model,
                                  extra_tools=[], memory_store=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://test") as c:
        headers = {**alice.auth, "Idempotency-Key": uuid.uuid4().hex}
        suspended(await c.post("/api/v1/agent/tasks", json={"input": "x"}, headers=headers))
        assert (await c.post(f"{CONTROL}/global-clear", headers=SU, json={})).json()["changed"] is True
        h.model.push(final("ok"))
        ok = await c.post("/api/v1/agent/tasks", json={"input": "x"},
                          headers={**alice.auth, "Idempotency-Key": uuid.uuid4().hex})
        assert ok.status_code == 200, ok.text


async def test_a_global_stop_whose_write_fails_still_refuses_submissions(h, monkeypatch):
    """The in-process latch is set before the row is written. If the write
    fails, the operator sees an error — and the process is still latched, not
    silently open."""

    alice = await h.user("alice")
    import server.composition.supervisor as supervisor

    def unwritable():
        # `_utcnow` is called only to build the row being persisted, so this
        # fails exactly the write — after the read, after the in-memory latch.
        raise RuntimeError("TEST-ONLY: the latch row cannot be written")

    monkeypatch.setattr(supervisor, "_utcnow", unwritable)
    try:
        resp = await global_stop(h)
        assert resp.status_code == 500
    except RuntimeError:
        pass  # the test transport re-raises app errors; either way the call failed
    monkeypatch.undo()

    assert await h.rows(SupervisorLatch) == []
    h.model.push(final("never"))
    suspended(await h.submit(alice))


async def test_an_unreadable_latch_refuses_submissions(h):
    alice = await h.user("alice")
    async with h.storage.session() as s:
        await s.execute(text("DROP TABLE supervisor_latch"))
        await s.commit()

    h.model.push(final("never"))
    suspended(await h.submit(alice))
    assert await h.rows(AgentTask) == []


async def test_a_task_created_during_a_global_stop_is_stopped(h, monkeypatch):
    """The race the synchronous re-check in `submit` closes: the gate read said
    open, but the latch was set before the task was registered. The task is
    stopped at once instead of running."""

    alice = await h.user("alice")

    async def open_(self) -> bool:
        return True

    monkeypatch.setattr(SupervisorGate, "submissions_open", open_)
    monkeypatch.setattr(SupervisorGate, "latched_now", lambda self: True)
    h.model.push(final("never"))

    assert "suspended all tasks" in stopped_task(await h.submit(alice))
    assert h.model.seen == []


# ── ordinary use ───────────────────────────────────────────────────────────


async def test_ordinary_use_is_unchanged_once_cleared(h):
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    await global_stop(h)
    await global_clear(h)

    paused = await paused_task(h, alice)
    ok = await h.confirm(alice, paused["task_id"], paused["confirmation_token"])
    assert ok.status_code == 200, ok.text
    assert len(h.ui.calls) == 1

    cancel_me = await paused_task(h, alice)
    resp = await h.client.post(f"/api/v1/agent/tasks/{cancel_me['task_id']}/cancel", headers=alice.auth)
    assert resp.json()["status"] == "cancelled"
    assert (await h.client.get("/api/v1/capabilities", headers=alice.auth)).status_code == 200


# ── import boundaries ──────────────────────────────────────────────────────

CONTRACT = "Only the gateway reaches superuser authority (12 §4, SUPER-001)"


@pytest.mark.parametrize("module, line", [
    ("server/agent/runtime.py", "from server.gateway.routers.control import router\n"),
    ("server/tools/registry.py", "from server.gateway.control_port import ControlScope\n"),
    ("server/dashboard/__init__.py", "from server.gateway.superuser_auth import get_superuser\n"),
    ("server/memory/hydration.py", "import server.composition.latch\n"),
    # 20 §2.2: no worker, tool, or executor path reaches break-glass records.
    ("server/agent/runtime.py", "from server.composition.break_glass import BreakGlassRegistry\n"),
    ("server/execution/process.py", "import server.composition.break_glass\n"),
    ("server/tools/platforms.py", "from server.composition.break_glass import BreakGlassRecord\n"),
])
async def test_the_control_modules_are_import_restricted(tmp_path, module, line):
    script = Path(sys.executable).parent / "lint-imports"
    clean = subprocess.run([str(script), "--config", "pyproject.toml"], cwd=REPO_ROOT,
                           capture_output=True, text=True)
    assert f"{CONTRACT} KEPT" in clean.stdout, clean.stdout

    copy = tmp_path / "repo"
    shutil.copytree(REPO_ROOT / "server", copy / "server", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO_ROOT / "shared", copy / "shared", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(REPO_ROOT / "pyproject.toml", copy / "pyproject.toml")
    target = copy / module
    target.write_text(line + target.read_text())

    result = subprocess.run([str(script), "--config", "pyproject.toml", "--no-cache"],
                            cwd=copy, capture_output=True, text=True)
    assert result.returncode != 0
    assert f"{CONTRACT} BROKEN" in result.stdout
