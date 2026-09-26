"""Break-glass (unconfined) execution, end to end — build unit U6
(`docs/20_CONFINEMENT_BREAK_GLASS.md`, OD-EXEC-2).

Everything runs through the real application: real superuser authentication,
the real runtime, authorization engine and confirmation service, the real
`system.shell` tool and process executor, and the real record store.

**The host here is a disposable test host without Landlock** (OD-BG-2): the
`bg` fixture makes the kernel report no Landlock ABI. That is what makes the
two paths observable on any machine — a *confined* run fails closed
(`platform_unsupported`), so a run that succeeds and reads a file outside its
task temp can only have taken the break-glass path. It is also exactly the
situation 20 §2.5 describes: on such a host normal `system.restricted` is
unsupported, and a per-task break-glass activation is the only way to run
anything.

| Acceptance hook | Test |
|---|---|
| BG-T1 off by default: nothing runs unconfined | `test_break_glass_is_off_by_default` |
| BG-T3 users cannot activate; a superuser can | `test_an_ordinary_user_cannot_activate_break_glass`, `test_a_superuser_activates_and_the_approved_run_is_unconfined` |
| BG-T4 no proposal can ask for it | `test_a_proposal_naming_confinement_is_malformed` (+ `tests/execution/test_break_glass_executor.py`) |
| BG-T5 activation + confirmation + step-up still required | `test_a_record_does_not_skip_activation_confirmation_or_step_up` |
| BG-T6 executable must be on the break-glass list and in the record | `test_executables_outside_the_record_or_the_list_are_not_unconfined` |
| BG-T7 ends at max_invocations / expiry / task end / revoke / breaker trip | `test_the_record_ends_when_*` |
| BG-T8 limits and /cancel apply to unconfined runs | `test_cancel_kills_an_unconfined_process`, `test_an_operator_stop_kills_an_unconfined_process`, `test_the_timeout_applies_to_an_unconfined_run` (+ executor tests) |
| BG-T9 activation / invocation / end audited; status observable | `test_a_superuser_activates_and_the_approved_run_is_unconfined` |
| BG-T10 BR-T2 with break-glass active | `tests/integration/test_br_t2_execution_rows.py` |
| worker / recovery cannot activate | `test_no_worker_or_recovery_path_can_create_a_record`, `test_a_worker_switch_never_touches_break_glass` |
| normal allow-list not widened | `test_enabling_break_glass_does_not_widen_the_normal_allow_list` |
| file / network boundaries unaffected | `test_file_and_network_boundaries_hold_while_a_record_is_live` |
| task / user / executable mismatch; limits | `test_activation_is_bound_to_a_live_task_its_owner_and_the_limits` |
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import update

from server.agent import ports as agent_ports
from server.composition.break_glass import BreakGlassRefused, RefusalCode
from server.execution import confinement
from server.execution.process import argv_digest
from server.models.provider import ModelUnavailable
from server.secrets.crypto import hash_token
from server.security.events import AuditAction
from server.security.superuser import SUPERUSER_TOKEN_ENV
from server.storage.models import AccessToken, AuditEvent
from shared.schemas.enums import AuditActor, AuditResult
from tests.runtime.conftest import (  # noqa: F401
    ScriptedModel,
    ask,
    call,
    failure_of,
    final,
    make_harness,
    pending_of,
)

pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "TEST-ONLY-superuser-credential-0123456789abcdef"
SU = {"Authorization": f"Superuser {TOKEN}"}
CONTROL = "/api/v1/admin/control"
API = "/api/v1"
OUTSIDE = "TEST-ONLY text outside the task temp"


def config(tmp_path: Path, *, enabled: bool = True, allowed=("cat", "sleep", "sh"), normal=(),
           **break_glass) -> dict:
    return {
        "execution": {
            "filesystem": {"base_root": str(tmp_path / "sandboxes")},
            "network": {"default_internet": True},
            "process": {
                "allowed_executables": list(normal),
                "max_timeout_seconds": 60,
                "break_glass": {"enabled": enabled, "allowed_executables": list(allowed),
                                "max_invocations": 3, **break_glass},
            },
        }
    }


@pytest_asyncio.fixture
async def bg(make_harness, monkeypatch, tmp_path):
    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, TOKEN)
    # A disposable test host without Landlock (OD-BG-2): confined runs fail closed.
    monkeypatch.setattr(confinement, "landlock_abi", lambda: 0)
    outside = tmp_path / "outside.txt"
    outside.write_text(OUTSIDE)

    async def _make(*, agent: dict | None = None, models: dict | None = None, **kwargs):
        cfg = config(tmp_path, **kwargs)
        if agent:
            cfg["agent"] = agent
        h = await make_harness(config=cfg, use_real_execution_tools=True, models=models)
        h.outside = str(outside)
        return h

    return _make


async def paused_shell(h, actor, argv: list[str], *, then=()) -> dict:
    h.model.push(ask("system.restricted"), call("system.shell", "run_shell_command", args={"argv": argv}), *then)
    return pending_of(await h.submit(actor))


async def activate(h, task_id, user_id, *, executables=("cat",), headers=SU, reason="recover_sandbox",
                   **extra):
    return await h.client.post(f"{CONTROL}/break-glass", headers=headers, json={
        "task_id": str(task_id), "user_id": str(user_id), "executables": list(executables),
        "reason": reason, **extra,
    })


async def revoke(h, task_id, headers=SU):
    return await h.client.post(f"{CONTROL}/break-glass/revoke", headers=headers,
                               json={"task_id": str(task_id), "reason": "operator_done"})


async def listed(h) -> list[dict]:
    resp = await h.client.get(f"{CONTROL}/break-glass", headers=SU)
    assert resp.status_code == 200, resp.text
    return resp.json()["records"]


async def audit(h, action: AuditAction) -> list[AuditEvent]:
    return [r for r in await h.rows(AuditEvent) if r.action == action.value]


async def tool_outcomes(h) -> list[str]:
    return [("ok " if r.action == AuditAction.AGENT_TOOL_EXECUTED.value else "failed ") + r.resource
            for r in await h.rows(AuditEvent)
            if r.action in (AuditAction.AGENT_TOOL_EXECUTED.value, AuditAction.AGENT_TOOL_FAILED.value)]


async def ended_reasons(h) -> list[str]:
    return [r.resource.rsplit(":", 1)[1] for r in await audit(h, AuditAction.BREAK_GLASS_ENDED)]


async def active(h, actor, task_id) -> bool:
    resp = await h.get(actor, task_id)
    assert resp.status_code == 200, resp.text
    return resp.json()["break_glass_active"]


# ── off by default (BG-T1) ─────────────────────────────────────────────────


async def test_break_glass_is_off_by_default(make_harness, monkeypatch, tmp_path):
    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, TOKEN)
    monkeypatch.setattr(confinement, "landlock_abi", lambda: 0)
    h = await make_harness(config={"execution": {
        "filesystem": {"base_root": str(tmp_path / "sandboxes")},
        "process": {"allowed_executables": ["cat"]},
    }}, use_real_execution_tools=True)
    assert h.config.execution.process.break_glass.enabled is False
    alice = await h.user("alice")
    await h.grant(alice, "system.restricted")
    paused = await paused_shell(h, alice, ["cat", "/etc/hostname"], then=[final("done")])

    resp = await activate(h, paused["task_id"], alice.user_id)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"]["refusal"] == "disabled"
    [refused] = await audit(h, AuditAction.CONTROL_BREAK_GLASS)
    assert (refused.actor, refused.result) == (AuditActor.SUPERUSER.value, AuditResult.BLOCKED.value)
    assert await audit(h, AuditAction.BREAK_GLASS_ACTIVATED) == []

    # The approved run takes the only path there is: confined, which this
    # host cannot do — so nothing runs.
    assert (await h.confirm(alice, paused["task_id"], paused["confirmation_token"])).status_code == 200
    assert await tool_outcomes(h) == ["failed tool:system.shell.run_shell_command:platform_unsupported"]
    assert h.break_glass.active() == [] and await listed(h) == []


# ── who can activate (BG-T3) ───────────────────────────────────────────────


async def test_an_ordinary_user_cannot_activate_break_glass(bg):
    h = await bg()
    alice = await h.user("alice")
    await h.grant(alice, "system.restricted")
    paused = await paused_shell(h, alice, ["cat", h.outside])

    for headers in (alice.auth,                                      # a user's Bearer token
                    {"Authorization": f"Superuser {alice.token}"},     # ... presented as superuser
                    {"Authorization": f"Superuser {TOKEN[:-1]}X"},     # a wrong superuser credential
                    {}):
        resp = await activate(h, paused["task_id"], alice.user_id, headers=headers)
        assert resp.status_code == 401, (headers, resp.text)
    for path, method in (("/break-glass/revoke", "post"), ("/break-glass", "get")):
        resp = await getattr(h.client, method)(f"{CONTROL}{path}", headers=alice.auth,
                                               **({"json": {"task_id": paused["task_id"], "reason": "x"}}
                                                  if method == "post" else {}))
        assert resp.status_code == 401, resp.text
    assert h.break_glass.active() == []
    assert await audit(h, AuditAction.BREAK_GLASS_ACTIVATED) == []


async def test_a_superuser_activates_and_the_approved_run_is_unconfined(bg):
    h = await bg()
    alice = await h.user("alice")
    await h.grant(alice, "system.restricted")
    paused = await paused_shell(h, alice, ["cat", h.outside], then=[final("done")])
    task_id = paused["task_id"]
    assert await active(h, alice, task_id) is False

    resp = await activate(h, task_id, alice.user_id)
    assert resp.status_code == 200, resp.text
    record = resp.json()
    assert (record["task_id"], record["user_id"]) == (task_id, str(alice.user_id))
    assert (record["executables"], record["max_invocations"], record["remaining"]) == (["cat"], 1, 1)
    assert record["reason"] == "recover_sandbox"
    window = datetime.fromisoformat(record["expires_at"]) - datetime.fromisoformat(record["activated_at"])
    # Capped by the task's own remaining time (120 s wall clock), not only by 15 min.
    assert timedelta(0) < window <= timedelta(seconds=h.config.agent.bounds.wall_clock_timeout_seconds)
    assert await active(h, alice, task_id) is True
    assert [r["record_id"] for r in await listed(h)] == [record["record_id"]]

    confirmed = await h.confirm(alice, task_id, paused["confirmation_token"])
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "completed"
    assert confirmed.json()["break_glass_active"] is False

    # It ran unconfined: this host cannot confine, and the child read a file
    # outside its task temp.
    assert await tool_outcomes(h) == ["ok tool:system.shell.run_shell_command"]
    assert OUTSIDE in h.model.all_text()

    rid = record["record_id"]
    [activated] = await audit(h, AuditAction.BREAK_GLASS_ACTIVATED)
    assert (activated.actor, activated.result) == (AuditActor.SUPERUSER.value, AuditResult.SUCCESS.value)
    assert activated.user_id == alice.user_id  # attributed to the task's owner
    assert activated.resource.startswith(f"bg:{rid}:{task_id}:recover_sandbox:n1:t")
    assert activated.resource.endswith(":cat")
    [invoked] = await audit(h, AuditAction.BREAK_GLASS_INVOKED)
    assert (invoked.actor, invoked.user_id) == (AuditActor.SYSTEM.value, alice.user_id)
    prefix = f"bg:{rid}:{task_id}:exited:0:"
    assert invoked.resource.startswith(prefix)
    duration, digest, exe = invoked.resource[len(prefix):].split(":")
    assert duration.endswith("ms") and exe == "cat"
    assert digest == argv_digest(["cat", h.outside])
    assert h.outside not in invoked.resource  # the command line is never in the audit trail
    assert await ended_reasons(h) == ["exhausted"]
    assert h.break_glass.active() == []
    assert all(TOKEN not in r.resource for r in await h.rows(AuditEvent))


# ── BG-T4: no proposal can ask for it ──────────────────────────────────────


async def test_a_proposal_naming_confinement_is_malformed(bg):
    h = await bg()
    alice = await h.user("alice")
    await h.grant(alice, "system.restricted")
    shell = {"argv": ["cat", h.outside]}
    malformed = [
        call("system.shell", "run_shell_command", args={**shell, "confinement_mode": "unconfined"}),
        call("system.shell", "run_shell_command", args={**shell, "env_overrides": {"BREAK_GLASS": "1"}}),
        call("system.shell", "run_shell_command", args=shell, break_glass=True),
        call("system.shell", "run_shell_command", args=shell, confinement="none"),
        call("system.shell", "run_shell_command", args={**shell, "options": {"Un-Confined": True}}),
        call("system.shell", "run_shell_command", args=shell, scope={"landlock": "off"}),
    ]
    for proposal in malformed:
        h.model.push(ask("system.restricted"), proposal, final("gave up"))
        resp = await h.submit(alice)
        assert resp.status_code == 200, resp.text
        assert resp.json()["pending"] is None  # nothing even reached a confirmation

    assert h.model.all_text().count("not a valid proposal") == len(malformed)
    assert await tool_outcomes(h) == []
    assert h.break_glass.active() == [] and await audit(h, AuditAction.BREAK_GLASS_INVOKED) == []


# ── BG-T5: everything else still applies ───────────────────────────────────


async def test_a_record_does_not_skip_activation_confirmation_or_step_up(bg):
    h = await bg()
    alice = await h.user("alice")
    await h.grant(alice, "system.restricted")
    paused = await paused_shell(h, alice, ["cat", h.outside], then=[
        # after the first (declined) run: a second shell call with no confirmation yet
        call("system.shell", "run_shell_command", args={"argv": ["cat", h.outside]}),
    ])
    task_id = paused["task_id"]
    assert (await activate(h, task_id, alice.user_id, max_invocations=3)).status_code == 200

    # Step-up: a stale session cannot approve, record or not.
    async with h.storage.session() as s:
        await s.execute(update(AccessToken).where(AccessToken.token_hash == hash_token(alice.token))
                        .values(issued_at=datetime.now(timezone.utc) - timedelta(minutes=30)))
        await s.commit()
    stale = await h.confirm(alice, task_id, paused["confirmation_token"])
    assert stale.status_code == 401 and stale.json()["error"]["details"].get("step_up_required") is True
    assert h.break_glass.active()[0].remaining == 3  # nothing was spent

    # Confirmation: declined is declined. The next run pauses for its own.
    fresh = await h.user("alice")
    declined = await h.confirm(fresh, task_id, paused["confirmation_token"], approve=False)
    second = pending_of(declined)
    assert second["pending"]["risk_category"] == "high_irreversible"
    assert second["pending"]["requires_step_up"] is True
    assert await tool_outcomes(h) == []
    assert h.break_glass.active()[0].remaining == 3

    # Activation: without system.restricted active for the task, nothing runs.
    bob = await h.user("bob")
    await h.grant(bob, "system.restricted")
    h.model.push(call("system.shell", "run_shell_command", args={"argv": ["cat", h.outside]}), final("no"))
    no_activation = await h.submit(bob)
    assert no_activation.status_code == 200 and no_activation.json()["pending"] is None
    assert "is not active for this task" in h.model.all_text()
    assert OUTSIDE not in h.model.all_text()


# ── BG-T6 / allow-lists ────────────────────────────────────────────────────


async def test_executables_outside_the_record_or_the_list_are_not_unconfined(bg):
    h = await bg(normal=("sh",))
    alice = await h.user("alice")
    await h.grant(alice, "system.restricted")

    # Not on break_glass.allowed_executables: refused at activation.
    paused = await paused_shell(h, alice, ["sh", "-c", f"cat {h.outside}"], then=[final("done")])
    refused = await activate(h, paused["task_id"], alice.user_id, executables=["id"])
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["details"]["refusal"] == "executable_not_allowed"

    # On the list, but not in this record (and on the normal list): the run
    # takes the ordinary, confined path — which fails closed here.
    assert (await activate(h, paused["task_id"], alice.user_id, executables=["cat"])).status_code == 200
    assert (await h.confirm(alice, paused["task_id"], paused["confirmation_token"])).status_code == 200
    assert await tool_outcomes(h) == ["failed tool:system.shell.run_shell_command:platform_unsupported"]
    assert await audit(h, AuditAction.BREAK_GLASS_INVOKED) == []
    assert OUTSIDE not in h.model.all_text()


async def test_enabling_break_glass_does_not_widen_the_normal_allow_list(bg):
    h = await bg(allowed=("cat",), normal=())
    alice = await h.user("alice")
    await h.grant(alice, "system.restricted")
    paused = await paused_shell(h, alice, ["cat", h.outside], then=[final("done")])

    assert (await h.confirm(alice, paused["task_id"], paused["confirmation_token"])).status_code == 200
    assert await tool_outcomes(h) == ["failed tool:system.shell.run_shell_command:unauthorized_executable"]


# ── activation binding and limits ──────────────────────────────────────────


async def test_activation_is_bound_to_a_live_task_its_owner_and_the_limits(bg):
    h = await bg(max_window_minutes=5)
    alice, bob = await h.user("alice"), await h.user("bob")
    await h.grant(alice, "system.restricted")
    paused = await paused_shell(h, alice, ["cat", h.outside], then=[final("done")])
    task_id = paused["task_id"]

    async def refusal(resp, status: int) -> str:
        assert resp.status_code == status, resp.text
        return resp.json()["error"]["details"]["refusal"]

    assert await refusal(await activate(h, task_id, bob.user_id), 409) == "user_mismatch"
    assert await refusal(await activate(h, uuid.uuid4(), alice.user_id), 409) == "task_not_live"
    assert await refusal(await activate(h, task_id, alice.user_id, max_invocations=4), 422) == "invalid_limits"
    assert await refusal(await activate(h, task_id, alice.user_id, expires_in_seconds=301), 422) == "invalid_limits"
    for bad in ({"reason": "Because I said so"}, {"reason": ""}, {"executables": []},
                {"expires_in_seconds": 0}, {"max_invocations": 0}, {"confinement_mode": "unconfined"}):
        assert (await activate(h, task_id, alice.user_id, **bad)).status_code == 422, bad
    assert h.break_glass.active() == []
    assert len(await audit(h, AuditAction.CONTROL_BREAK_GLASS)) == 4  # the refusals that reached the store

    first = await activate(h, task_id, alice.user_id, expires_in_seconds=30)
    assert first.status_code == 200, first.text
    window = (datetime.fromisoformat(first.json()["expires_at"])
              - datetime.fromisoformat(first.json()["activated_at"]))
    assert window == timedelta(seconds=30)
    assert await refusal(await activate(h, task_id, alice.user_id), 409) == "already_active"

    # A finished task cannot be given one.
    assert (await h.confirm(alice, task_id, paused["confirmation_token"])).status_code == 200
    assert await refusal(await activate(h, task_id, alice.user_id), 409) == "task_not_live"


# ── BG-T7: the record ends ─────────────────────────────────────────────────


async def test_the_record_ends_when_its_invocations_are_spent(bg):
    h = await bg()
    alice = await h.user("alice")
    await h.grant(alice, "system.restricted")
    paused = await paused_shell(h, alice, ["cat", h.outside], then=[
        call("system.shell", "run_shell_command", args={"argv": ["cat", h.outside, h.outside]}),
    ])
    assert (await activate(h, paused["task_id"], alice.user_id)).status_code == 200  # max_invocations=1

    second = pending_of(await h.confirm(alice, paused["task_id"], paused["confirmation_token"]))
    assert await ended_reasons(h) == ["exhausted"]
    h.model.push(final("done"))
    assert (await h.confirm(alice, paused["task_id"], second["confirmation_token"])).status_code == 200
    assert await tool_outcomes(h) == [
        "ok tool:system.shell.run_shell_command",
        # the record is gone, and `cat` is only on the break-glass list
        "failed tool:system.shell.run_shell_command:unauthorized_executable",
    ]
    assert len(await audit(h, AuditAction.BREAK_GLASS_INVOKED)) == 1


async def test_the_record_ends_when_it_expires(bg):
    h = await bg()
    alice = await h.user("alice")
    await h.grant(alice, "system.restricted")
    paused = await paused_shell(h, alice, ["cat", h.outside], then=[final("done")])
    assert (await activate(h, paused["task_id"], alice.user_id)).status_code == 200

    later = datetime.now(timezone.utc) + timedelta(minutes=16)
    h.break_glass._clock = lambda: later
    assert await active(h, alice, paused["task_id"]) is False
    assert (await h.confirm(alice, paused["task_id"], paused["confirmation_token"])).status_code == 200
    assert await tool_outcomes(h) == ["failed tool:system.shell.run_shell_command:unauthorized_executable"]
    assert await ended_reasons(h) == ["expired"]


async def test_the_record_ends_when_the_task_ends(bg):
    h = await bg()
    alice = await h.user("alice")
    await h.grant(alice, "system.restricted")

    # completed: the approved run was declined, the task then finished
    paused = await paused_shell(h, alice, ["cat", h.outside], then=[final("fine")])
    assert (await activate(h, paused["task_id"], alice.user_id)).status_code == 200
    done = await h.confirm(alice, paused["task_id"], paused["confirmation_token"], approve=False)
    assert done.json()["status"] == "completed"

    # cancelled while paused
    cancelled = await paused_shell(h, alice, ["cat", h.outside])
    assert (await activate(h, cancelled["task_id"], alice.user_id)).status_code == 200
    resp = await h.client.post(f"{API}/agent/tasks/{cancelled['task_id']}/cancel", headers=alice.auth)
    assert resp.json()["status"] == "cancelled"

    assert await ended_reasons(h) == ["task_completed", "task_cancelled"]
    assert await audit(h, AuditAction.BREAK_GLASS_INVOKED) == []
    assert h.break_glass.active() == [] and await listed(h) == []


async def test_the_record_ends_when_a_superuser_revokes_it(bg):
    h = await bg()
    alice = await h.user("alice")
    await h.grant(alice, "system.restricted")
    paused = await paused_shell(h, alice, ["cat", h.outside], then=[final("done")])
    assert (await activate(h, paused["task_id"], alice.user_id)).status_code == 200

    assert (await revoke(h, paused["task_id"], headers=alice.auth)).status_code == 401
    resp = await revoke(h, paused["task_id"])
    assert resp.status_code == 200, resp.text
    assert await active(h, alice, paused["task_id"]) is False
    again = await revoke(h, paused["task_id"])
    assert again.status_code == 404, again.text

    assert (await h.confirm(alice, paused["task_id"], paused["confirmation_token"])).status_code == 200
    assert await tool_outcomes(h) == ["failed tool:system.shell.run_shell_command:unauthorized_executable"]
    assert await ended_reasons(h) == ["revoked"]
    control = [r.resource for r in await audit(h, AuditAction.CONTROL_BREAK_GLASS)]
    assert control == [f"control:break_glass:revoke:{paused['task_id']}:operator_done",
                       f"control:break_glass:revoke:{paused['task_id']}:operator_done:not_found"]


async def test_the_record_ends_when_the_breaker_trips(bg):
    h = await bg()
    alice = await h.user("alice")
    await h.grant(alice, "system.restricted")
    paused = await paused_shell(h, alice, ["cat", h.outside])
    assert (await activate(h, paused["task_id"], alice.user_id)).status_code == 200

    stop = await h.client.post(f"{CONTROL}/stop", headers=SU,
                               json={"scope": "task", "target_id": paused["task_id"], "reason": "enough"})
    assert stop.status_code == 200, stop.text

    assert h.break_glass.active() == []
    assert await ended_reasons(h) == ["breaker_trip"]
    refused = await h.confirm(alice, paused["task_id"], paused["confirmation_token"])
    assert refused.status_code == 409
    assert await tool_outcomes(h) == []


# ── BG-T8: an unconfined run is still bounded and cancellable ─────────────


def _processes_with(marker: str) -> list[int]:
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if marker.encode() in cmdline:
            found.append(int(entry.name))
    return found


async def _running_unconfined_sleep(h, alice, marker: str):
    await h.grant(alice, "system.restricted")
    paused = await paused_shell(h, alice, ["sleep", marker], then=[final("should not be reached")])
    assert (await activate(h, paused["task_id"], alice.user_id, executables=["sleep"])).status_code == 200
    confirming = asyncio.create_task(h.confirm(alice, paused["task_id"], paused["confirmation_token"]))
    for _ in range(100):
        if _processes_with(marker):
            break
        await asyncio.sleep(0.05)
    assert _processes_with(marker), "the unconfined process never started"
    return paused["task_id"], confirming


async def _gone(marker: str) -> bool:
    for _ in range(40):
        if not _processes_with(marker):
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.mark.skipif(not Path("/proc").exists(), reason="needs /proc to observe the child process")
async def test_cancel_kills_an_unconfined_process(bg):
    h = await bg()
    alice = await h.user("alice")
    marker = "46.375"
    started = time.monotonic()
    task_id, confirming = await _running_unconfined_sleep(h, alice, marker)

    cancel = await h.client.post(f"{API}/agent/tasks/{task_id}/cancel", headers=alice.auth)
    assert cancel.status_code == 200, cancel.text
    finished = await asyncio.wait_for(confirming, timeout=15)
    assert finished.json()["status"] == "cancelled"
    assert time.monotonic() - started < 15
    assert await _gone(marker), "an unconfined process outlived its cancelled task"

    [invoked] = await audit(h, AuditAction.BREAK_GLASS_INVOKED)
    assert ":cancelled:-:" in invoked.resource
    assert set(await ended_reasons(h)) == {"exhausted"}


@pytest.mark.skipif(not Path("/proc").exists(), reason="needs /proc to observe the child process")
async def test_an_operator_stop_kills_an_unconfined_process(bg):
    h = await bg()
    alice = await h.user("alice")
    marker = "45.625"
    task_id, confirming = await _running_unconfined_sleep(h, alice, marker)

    stop = await h.client.post(f"{CONTROL}/stop", headers=SU,
                               json={"scope": "task", "target_id": task_id, "reason": "runaway"})
    assert stop.status_code == 200, stop.text
    finished = await asyncio.wait_for(confirming, timeout=15)
    assert failure_of(finished) == "emergency_stop"
    assert await _gone(marker), "an unconfined process outlived the breaker trip"
    [invoked] = await audit(h, AuditAction.BREAK_GLASS_INVOKED)
    assert ":cancelled:-:" in invoked.resource


async def test_the_timeout_applies_to_an_unconfined_run(bg):
    h = await bg()
    alice = await h.user("alice")
    await h.grant(alice, "system.restricted")
    h.model.push(ask("system.restricted"),
                 call("system.shell", "run_shell_command", args={"argv": ["sleep", "30"], "timeout": 1}),
                 final("done"))
    paused = pending_of(await h.submit(alice))
    assert (await activate(h, paused["task_id"], alice.user_id, executables=["sleep"])).status_code == 200

    started = time.monotonic()
    assert (await h.confirm(alice, paused["task_id"], paused["confirmation_token"])).status_code == 200
    assert time.monotonic() - started < 10
    [invoked] = await audit(h, AuditAction.BREAK_GLASS_INVOKED)
    assert ":timed_out:-1:" in invoked.resource


# ── other boundaries are untouched ─────────────────────────────────────────


async def test_file_and_network_boundaries_hold_while_a_record_is_live(bg):
    h = await bg()
    alice = await h.user("alice")
    notes = {"sandbox_root": "notes"}
    await h.grant(alice, "system.restricted")
    await h.grant(alice, "file.read", resource_scope=notes)
    await h.grant(alice, "net.request")
    h.model.push(
        ask("system.restricted"), ask("file.read", scope=notes), ask("net.request"),
        call("system.shell", "run_shell_command", args={"argv": ["cat", h.outside]}),
    )
    paused = pending_of(await h.submit(alice))
    assert (await activate(h, paused["task_id"], alice.user_id, max_invocations=2)).status_code == 200
    h.model.push(
        call("files.read", "read_file", args={"relative_path": "../../../../../etc/passwd"}),
        call("net.request", "get", args={"url": "http://169.254.169.254/latest/meta-data/"}),
        final("done"),
    )
    assert (await h.confirm(alice, paused["task_id"], paused["confirmation_token"])).status_code == 200

    assert await tool_outcomes(h) == [
        "ok tool:system.shell.run_shell_command",
        "failed tool:files.read.read_file:forbidden_path",
        "failed tool:net.request.get:egress_denied",
    ]
    assert "root:" not in h.model.all_text()


# ── no worker, model or recovery path reaches activation ──────────────────


async def test_no_worker_or_recovery_path_can_create_a_record(bg):
    """Structural: the runtime is handed a port with no way to create a record,
    the executor a Protocol that can only claim one, and neither may import the
    store (pyproject contract). Creation needs a verified superuser."""

    port_methods = {n for n, _ in inspect.getmembers(agent_ports.SecurityPort, inspect.isfunction)}
    assert {n for n in port_methods if "break" in n or "glass" in n} == {"settle_break_glass"}
    from server.execution import break_glass as executor_side

    assert {n for n in dir(executor_side.BreakGlassLookup) if not n.startswith("_")} == {
        "claim", "record_invocation"}
    for package in ("agent", "tools", "execution", "models", "modeltools", "memory"):
        for source in (REPO_ROOT / "server" / package).rglob("*.py"):
            text = source.read_text()
            assert "BreakGlassRegistry" not in text and "composition.break_glass" not in text, source
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert '"server.composition.break_glass",' in pyproject

    h = await bg()
    with pytest.raises(BreakGlassRefused) as excinfo:
        h.break_glass.prepare(object(), task_id=uuid.uuid4(), task_owner=uuid.uuid4(), user_id=uuid.uuid4(),
                              executables=["cat"], max_invocations=1, window_seconds=None,
                              task_seconds_left=60, reason="x")
    assert excinfo.value.code is RefusalCode.NOT_SUPERUSER


async def test_a_worker_switch_never_touches_break_glass(bg):
    """18 §4 recovery replaces the worker; it never activates, extends or
    re-enables break-glass — only the operator does."""

    w2 = ScriptedModel("w2")
    h = await bg(agent={"recovery": {"chain": [{"provider": "ollama", "model": "w2"}]}}, models={"w2": w2})
    alice = await h.user("alice")
    await h.grant(alice, "system.restricted")
    paused = await paused_shell(h, alice, ["cat", h.outside], then=[ModelUnavailable("primary down")])
    assert (await activate(h, paused["task_id"], alice.user_id, max_invocations=2)).status_code == 200
    [before] = h.break_glass.active()
    expires = before.expires_at
    w2.push(call("system.shell", "run_shell_command", args={"argv": ["cat", h.outside, "-"]}))

    second = pending_of(await h.confirm(alice, paused["task_id"], paused["confirmation_token"]))

    assert len(await audit(h, AuditAction.AGENT_WORKER_SWITCHED)) == 1
    [after] = h.break_glass.active()
    assert (after.record_id, after.remaining, after.expires_at) == (before.record_id, 1, expires)
    assert len(await audit(h, AuditAction.BREAK_GLASS_ACTIVATED)) == 1
    # The new worker's run still needs the owner's own confirmation.
    assert second["pending"]["risk_category"] == "high_irreversible"


async def test_a_trip_ends_the_record_before_the_stop_is_enforced(bg):
    """The operator path ends the record in memory the moment it trips the
    task — before any request enforces the stop — so a running task cannot
    claim it in between."""

    h = await bg()
    alice = await h.user("alice")
    await h.grant(alice, "system.restricted")
    paused = await paused_shell(h, alice, ["cat", h.outside])
    assert (await activate(h, paused["task_id"], alice.user_id, max_invocations=2)).status_code == 200
    state = h.runtime.states.get(uuid.UUID(paused["task_id"]))

    h.app.state.supervisor_control._trip_live([state], reason="runaway", source="operator")

    assert state.stop_enforced is False  # nothing has enforced the stop yet
    assert h.break_glass.active() == []
    assert h.break_glass.claim(task_id=state.task_id, user_id=alice.user_id, executable="cat") is None


@pytest.mark.parametrize("condition", ["tripped", "cancelled", "latched"])
async def test_a_stopped_or_cancelled_task_cannot_be_given_a_record(bg, condition):
    h = await bg()
    alice = await h.user("alice")
    await h.grant(alice, "system.restricted")
    paused = await paused_shell(h, alice, ["cat", h.outside])
    task_id = uuid.UUID(paused["task_id"])
    if condition == "tripped":
        h.runtime.signal_stop(task_id, reason="runaway", source="operator")
    elif condition == "cancelled":
        h.runtime.states.get(task_id).cancelled = True
    else:
        h.app.state.supervisor_control._latch.latched = True

    resp = await activate(h, task_id, alice.user_id)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"]["refusal"] == "task_not_live"
    assert h.break_glass.active() == []


async def test_cancelling_a_task_with_no_live_state_ends_its_record(bg):
    """A task whose in-process state is gone (pruned while paused) still ends
    its record when cancelled from its row."""

    h = await bg()
    alice = await h.user("alice")
    await h.grant(alice, "system.restricted")
    paused = await paused_shell(h, alice, ["cat", h.outside])
    assert (await activate(h, paused["task_id"], alice.user_id)).status_code == 200
    h.runtime.states.pop(uuid.UUID(paused["task_id"]))

    resp = await h.client.post(f"{API}/agent/tasks/{paused['task_id']}/cancel", headers=alice.auth)
    assert resp.json()["status"] == "cancelled"
    assert h.break_glass.active() == []
    assert await ended_reasons(h) == ["task_cancelled"]
