"""docs/32 ([PROPOSAL] OD-AUTO-1) end to end: the agent proposes a workflow,
Track B authorizes and asks the human, and only then does n8n run it.

Runs the production composition root with the real Security Core,
confirmation tokens and the n8n executor. The model is scripted (it is the
untrusted component). n8n is an in-process fake by default; set
`JARVIS_N8N_LIVE_TOKEN_FILE` to a file holding the webhook token to run the
same cases against a real n8n on 127.0.0.1:5678 with the `jarvis-echo` and
`jarvis-fail` workflows (docs/32 §5).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from server.execution.automation import InputField, N8nWorkflowExecutor, PinnedWorkflow
from server.security.events import AuditAction
from server.storage.models import AuditEvent, CapabilityGrant, ConfirmationToken, UsageEvent
from server.tools.platforms import automation_workflow_tool
from shared.schemas.enums import UsageKind
from tests.runtime.conftest import ask, call, final, pending_of

LIVE_TOKEN_FILE = os.environ.get("JARVIS_N8N_LIVE_TOKEN_FILE")
FAKE_TOKEN = "fake-n8n-token-for-tests"

ECHO = PinnedWorkflow(
    id="jarvis.echo", webhook_path="jarvis-echo", description="Echo and transform a message (test only).",
    inputs={"message": InputField("string", max_length=200), "count": InputField("integer", required=False)},
)
FAIL = PinnedWorkflow(id="jarvis.fail", webhook_path="jarvis-fail", description="Always fails (test only).", inputs={})


class FakeN8n:
    """Mirrors the two validation workflows' observable behaviour."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("X-Jarvis-Webhook-Token") != FAKE_TOKEN:
            return httpx.Response(403, text="Authorization data is wrong!")
        body = json.loads(request.content)
        self.calls.append({"path": request.url.path, "body": body})
        if request.url.path == "/webhook/jarvis-echo":
            msg = body["inputs"]["message"]
            return httpx.Response(200, json={"ok": True, "echo": msg, "transformed": {"upper": msg.upper()}})
        if request.url.path == "/webhook/jarvis-fail":
            return httpx.Response(500, json={"ok": False, "error": "simulated failure"})
        return httpx.Response(404, json={"code": 404})


@pytest.fixture
def n8n():
    if LIVE_TOKEN_FILE:
        token = Path(LIVE_TOKEN_FILE).read_text().strip()
        fake = None
        transport = None
    else:
        token = FAKE_TOKEN
        fake = FakeN8n()
        transport = httpx.MockTransport(fake)

    async def source() -> str:
        return token

    executor = N8nWorkflowExecutor(base_url="http://127.0.0.1:5678", workflows=[ECHO, FAIL], token=source,
                                   transport=transport)
    return executor, fake


def completed(resp: httpx.Response) -> dict:
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed", body
    return body


async def tool_calls(h) -> list[UsageEvent]:
    return [u for u in await h.rows(UsageEvent) if u.kind is UsageKind.TOOL_CALL]


async def actions(h) -> list[str]:
    return [row.action for row in await h.rows(AuditEvent)]


ONLY_ECHO = {"workflow_id": "jarvis.echo"}


async def test_an_approved_workflow_runs_only_after_the_human_confirms(make_harness, n8n):
    executor, fake = n8n
    h = await make_harness(extra_tools=[automation_workflow_tool(executor)])
    alice = await h.user("alice")
    await h.grant(alice, "automation.workflow", resource_scope=ONLY_ECHO)

    h.model.push(
        ask("automation.workflow", scope=ONLY_ECHO),
        call("automation.run_workflow", "run_workflow",
             args={"workflow_id": "jarvis.echo", "inputs": {"message": "validation run"}}),
    )
    paused = pending_of(await h.submit(alice, "run the echo workflow with 'validation run'"))
    # Deterministic tiering paused it; the card shows the exact workflow and inputs.
    assert paused["pending"]["risk_category"] == "consequential"
    assert paused["pending"]["operation"] == "run_workflow"
    assert paused["pending"]["arguments"] == {"workflow_id": "jarvis.echo", "inputs": {"message": "validation run"}}
    assert await tool_calls(h) == []
    if fake is not None:
        assert fake.calls == []

    h.model.push(final("The workflow echoed your message."))
    body = completed(await h.confirm(alice, paused["task_id"], paused["confirmation_token"]))
    assert body["response"] == "The workflow echoed your message."
    # The workflow's result reached the model as an observation (data, not authority).
    assert "VALIDATION RUN" in h.model.all_text()
    assert len(await tool_calls(h)) == 1
    recorded = await actions(h)
    assert AuditAction.CONFIRMATION_ACCEPTED.value in recorded
    assert recorded.count(AuditAction.AGENT_TOOL_EXECUTED.value) == 1
    if fake is not None:
        assert [c["path"] for c in fake.calls] == ["/webhook/jarvis-echo"]
        # n8n is told nothing about who the user is.
        assert set(fake.calls[0]["body"]) == {"workflow_id", "inputs", "jarvis"}


async def test_without_a_grant_nothing_reaches_n8n(make_harness, n8n):
    executor, fake = n8n
    h = await make_harness(extra_tools=[automation_workflow_tool(executor)])
    mallory = await h.user("mallory")  # no grant

    h.model.push(
        call("automation.run_workflow", "run_workflow",
             args={"workflow_id": "jarvis.echo", "inputs": {"message": "x"}}),
        ask("automation.workflow", scope=ONLY_ECHO),
    )
    paused = pending_of(await h.submit(mallory))
    assert paused["pending"]["kind"] == "capability_activation"
    h.model.push(final("not running anything"))
    completed(await h.confirm(mallory, paused["task_id"], paused["confirmation_token"], approve=False))

    assert await tool_calls(h) == []
    assert await h.rows(CapabilityGrant) == []
    assert AuditAction.AGENT_TOOL_EXECUTED.value not in await actions(h)
    if fake is not None:
        assert fake.calls == []


async def test_a_grant_for_one_workflow_does_not_cover_another(make_harness, n8n):
    executor, fake = n8n
    h = await make_harness(extra_tools=[automation_workflow_tool(executor)])
    alice = await h.user("alice")
    await h.grant(alice, "automation.workflow", resource_scope=ONLY_ECHO)

    h.model.push(
        ask("automation.workflow", scope=ONLY_ECHO),
        # The model names a different pinned workflow than the grant covers.
        call("automation.run_workflow", "run_workflow", args={"workflow_id": "jarvis.fail", "inputs": {}}),
    )
    paused = pending_of(await h.submit(alice))
    h.model.push(final("that did not run"))
    completed(await h.confirm(alice, paused["task_id"], paused["confirmation_token"]))
    # Even an approved card cannot widen the grant: the executor refuses.
    assert AuditAction.AGENT_TOOL_EXECUTED.value not in await actions(h)
    if fake is not None:
        assert fake.calls == []


async def test_an_unpinned_or_malformed_request_is_refused_before_n8n(make_harness, n8n):
    executor, fake = n8n
    h = await make_harness(extra_tools=[automation_workflow_tool(executor)])
    alice = await h.user("alice")
    await h.grant(alice, "automation.workflow")

    h.model.push(
        ask("automation.workflow"),
        call("automation.run_workflow", "run_workflow",
             args={"workflow_id": "http://127.0.0.1:5678/webhook/anything", "inputs": {}}),
    )
    paused = pending_of(await h.submit(alice))
    h.model.push(
        call("automation.run_workflow", "run_workflow",
             args={"workflow_id": "jarvis.echo", "inputs": {"message": "x", "shell": "id"}}),
    )
    paused2 = pending_of(await h.confirm(alice, paused["task_id"], paused["confirmation_token"]))
    h.model.push(final("both refused"))
    completed(await h.confirm(alice, paused2["task_id"], paused2["confirmation_token"]))
    assert AuditAction.AGENT_TOOL_EXECUTED.value not in await actions(h)
    assert "invalid_arguments" in h.model.all_text()
    if fake is not None:
        assert fake.calls == []


async def test_a_failing_workflow_is_recorded_as_a_failed_tool_call(make_harness, n8n):
    executor, fake = n8n
    h = await make_harness(extra_tools=[automation_workflow_tool(executor)])
    alice = await h.user("alice")
    await h.grant(alice, "automation.workflow")

    h.model.push(
        ask("automation.workflow"),
        call("automation.run_workflow", "run_workflow", args={"workflow_id": "jarvis.fail", "inputs": {}}),
    )
    paused = pending_of(await h.submit(alice))
    h.model.push(final("the workflow failed"))
    body = completed(await h.confirm(alice, paused["task_id"], paused["confirmation_token"]))
    assert body["response"] == "the workflow failed"
    assert "workflow_failed" in h.model.all_text()
    assert AuditAction.AGENT_TOOL_FAILED.value in await actions(h)
    # Exactly one attempt: a workflow failure is never retried.
    if fake is not None:
        assert [c["path"] for c in fake.calls] == ["/webhook/jarvis-fail"]


async def test_a_confirmation_cannot_be_replayed_to_run_the_workflow_twice(make_harness, n8n):
    executor, fake = n8n
    h = await make_harness(extra_tools=[automation_workflow_tool(executor)])
    alice = await h.user("alice")
    await h.grant(alice, "automation.workflow", resource_scope=ONLY_ECHO)
    h.model.push(
        ask("automation.workflow", scope=ONLY_ECHO),
        call("automation.run_workflow", "run_workflow",
             args={"workflow_id": "jarvis.echo", "inputs": {"message": "once only"}}),
    )
    paused = pending_of(await h.submit(alice))
    h.model.push(final("ok"))
    completed(await h.confirm(alice, paused["task_id"], paused["confirmation_token"]))
    replay = await h.confirm(alice, paused["task_id"], paused["confirmation_token"])
    assert replay.status_code in (404, 409)
    assert len(await tool_calls(h)) == 1
    tokens = await h.rows(ConfirmationToken)
    assert all(t.used_at is not None for t in tokens)
    if fake is not None:
        assert len(fake.calls) == 1
