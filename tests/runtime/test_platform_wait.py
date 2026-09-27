"""docs/23 §5.3: the TASK may wait for an on-device dependency; the authorized
OPERATION may not.

A device operation refused `platform_unavailable` (Shizuku gone after a
reboot, the Accessibility service turned off) puts the task into a bounded
`waiting_for_platform` state. Nothing is queued on the device. When that
device reports the dependency available again, the call is proposed again
through every check and the engine — so a grant revoked meanwhile is honoured
and a consequential action asks for a new confirmation — and a fresh operation
is sent. A wait that runs out fails the task explicitly.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from server.security.audit import AuditLogger
from shared.schemas.agent import ToolInvocation, ToolOutput
from tests.runtime.conftest import android_ui_tool, ask, call, final, pending_of

SCOPE = {"package_name": "com.example"}
UNAVAILABLE = ToolOutput(ok=False, error="platform_unavailable", required_platform="shizuku")


class ScriptedDevice:
    """The android adapter: answers from a script, records every invocation."""

    def __init__(self, *outputs: ToolOutput) -> None:
        self.outputs = list(outputs)
        self.calls: list[ToolInvocation] = []

    async def execute(self, invocation: ToolInvocation) -> ToolOutput:
        self.calls.append(invocation)
        return self.outputs.pop(0) if self.outputs else ToolOutput(ok=True, content="done on device")


async def _setup(make_harness, *outputs: ToolOutput):
    device = ScriptedDevice(*outputs)
    h = await make_harness(extra_tools=[android_ui_tool(device)])
    alice = await h.user("alice")
    grant = await h.grant(alice, "app.interact", resource_scope=SCOPE)
    return h, alice, device, grant


async def _resume(h, device_id, dependency="shizuku"):
    async with h.storage.session() as db:
        results = await h.app.state.agent_tasks.resume_after_platform(
            db, device_id=device_id, dependency=dependency, audit=AuditLogger(db, request_id=uuid.uuid4()))
        await db.commit()
    return results


def _read(**kw):
    return call("ui.app", "read_screen_element", args={"view_id": "total"}, platform="android", **kw)


async def test_the_task_waits_and_nothing_is_retried_until_that_device_reports_back(make_harness):
    h, alice, device, _ = await _setup(make_harness, UNAVAILABLE)
    h.model.push(ask("app.interact", scope=SCOPE), _read(), final("The total is 42."))
    resp = await h.submit(alice, "what's the total?")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "waiting_for_platform"
    assert body["waiting_for"]["dependency"] == "shizuku"
    assert body["waiting_for"]["device_id"] == str(alice.device_id)
    assert len(device.calls) == 1

    task_id = body["task_id"]
    assert (await h.get(alice, task_id)).json()["status"] == "waiting_for_platform"
    # Another device, or another dependency, resumes nothing.
    assert await _resume(h, uuid.uuid4()) == []
    assert await _resume(h, alice.device_id, "accessibility_service") == []
    assert len(device.calls) == 1

    [result] = await _resume(h, alice.device_id)
    assert result.status.value == "completed" and result.response == "The total is 42."
    # A second invocation — a new operation, not the refused one re-sent.
    assert len(device.calls) == 2 and device.calls[1] is not device.calls[0]
    assert "available again" in h.model.all_text()
    # Nothing is left waiting.
    assert await _resume(h, alice.device_id) == []


async def test_resuming_reauthorizes_so_a_grant_revoked_meanwhile_is_honoured(make_harness):
    h, alice, device, grant = await _setup(make_harness, UNAVAILABLE)
    h.model.push(ask("app.interact", scope=SCOPE), _read(), final("I could not read it."))
    assert (await h.submit(alice)).json()["status"] == "waiting_for_platform"
    await h.revoke(alice, grant)

    [result] = await _resume(h, alice.device_id)
    assert result.status.value == "completed"
    assert len(device.calls) == 1  # the revoked authority sent nothing


async def test_a_consequential_call_needs_a_new_confirmation_after_the_wait(make_harness):
    h, alice, device, _ = await _setup(make_harness, UNAVAILABLE)
    h.model.push(ask("app.interact", scope=SCOPE),
                 call("ui.app", "input_text", args={"text": "milk"}, platform="android"), final("Typed."))
    first = pending_of(await h.submit(alice))
    confirmed = await h.confirm(alice, first["task_id"], first["confirmation_token"])
    assert confirmed.json()["status"] == "waiting_for_platform"
    assert len(device.calls) == 1

    [result] = await _resume(h, alice.device_id)
    # The spent approval does not carry across the wait.
    assert result.status.value == "awaiting_confirmation"
    assert result.pending.confirmation_token != first["confirmation_token"]
    assert len(device.calls) == 1
    done = await h.confirm(alice, first["task_id"], result.pending.confirmation_token)
    assert done.json()["status"] == "completed"
    assert len(device.calls) == 2


async def test_a_wait_that_runs_out_fails_the_task_and_never_retries(make_harness):
    h, alice, device, _ = await _setup(make_harness, UNAVAILABLE)
    h.model.push(ask("app.interact", scope=SCOPE), _read(), final("unused"))
    task_id = (await h.submit(alice)).json()["task_id"]
    state = h.runtime.states.get(uuid.UUID(task_id))
    state.platform_wait.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    body = (await h.get(alice, task_id)).json()
    assert body["status"] == "failed" and body["failure"]["code"] == "platform_unavailable"
    assert await _resume(h, alice.device_id) == []
    assert len(device.calls) == 1


async def test_waits_are_bounded_per_task(make_harness):
    h, alice, device, _ = await _setup(make_harness, UNAVAILABLE, UNAVAILABLE, UNAVAILABLE)
    h.model.push(ask("app.interact", scope=SCOPE), _read(), final("Shizuku is still off."))
    assert (await h.submit(alice)).json()["status"] == "waiting_for_platform"
    [second] = await _resume(h, alice.device_id)
    assert second.status.value == "waiting_for_platform"
    [third] = await _resume(h, alice.device_id)
    # Two waits used: the third refusal is an ordinary failed observation.
    assert third.status.value == "completed"
    assert len(device.calls) == 3


async def test_cancelling_a_waiting_task_drops_the_call(make_harness):
    h, alice, device, _ = await _setup(make_harness, UNAVAILABLE)
    h.model.push(ask("app.interact", scope=SCOPE), _read(), final("unused"))
    task_id = (await h.submit(alice)).json()["task_id"]
    cancelled = await h.client.post(f"/api/v1/agent/tasks/{task_id}/cancel", headers=alice.auth)
    assert cancelled.json()["status"] == "cancelled", cancelled.text
    assert await _resume(h, alice.device_id) == []
    assert len(device.calls) == 1


async def test_a_restart_fails_a_waiting_task_closed(make_harness):
    h, alice, device, _ = await _setup(make_harness, UNAVAILABLE)
    h.model.push(ask("app.interact", scope=SCOPE), _read(), final("unused"))
    task_id = (await h.submit(alice)).json()["task_id"]
    h.runtime.states.pop(uuid.UUID(task_id))  # the process forgot it
    async with h.storage.session() as db:
        closed = await h.app.state.agent_tasks.reconcile_after_restart(
            db, audit=AuditLogger(db, request_id=uuid.uuid4()))
        await db.commit()
    assert uuid.UUID(task_id) in closed
    assert (await h.get(alice, task_id)).json()["failure"]["code"] == "platform_unavailable"
    assert len(device.calls) == 1
