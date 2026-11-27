"""docs/32 ([PROPOSAL] OD-AUTO-1): the n8n execution target.

The executor is execution, not authority: these tests pin what it enforces on
its own (the pinned catalogue, typed inputs, the grant's workflow scope, size
caps, no redirects, dedupe, the one safe retry) and that the webhook token
never leaves through a result or an error. n8n itself is an httpx
MockTransport here; the live run is in the validation evidence.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest

from server.capabilities.registry import lookup
from server.config.schema import AppConfig, N8nConfig
from server.execution.automation import (
    IDEMPOTENCY_HEADER,
    InMemoryInvocationLedger,
    InputField,
    N8nWorkflowExecutor,
    PinnedWorkflow,
)
from server.tools.platforms import automation_workflow_tool
from server.tools.registry import ToolRegistry
from shared.schemas.agent import ExecutionPlatform, ToolInvocation
from shared.schemas.enums import RiskCategory
from shared.schemas.execution import ExecutionError, ExecutionErrorCode, ExecutionRequest



TOKEN = "test-webhook-token-not-a-real-secret-0123456789"

ECHO = PinnedWorkflow(
    id="jarvis.echo", webhook_path="jarvis-echo", description="echo",
    inputs={"message": InputField("string", max_length=40), "count": InputField("integer", required=False)},
)
FAIL = PinnedWorkflow(id="jarvis.fail", webhook_path="jarvis-fail", description="fails", inputs={})


async def _token() -> str:
    return TOKEN


class Recorder:
    def __init__(self, handler=None) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler or (lambda r: httpx.Response(200, json={"echo": json.loads(r.content)["inputs"]}))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def executor(recorder: Recorder, **kw) -> N8nWorkflowExecutor:
    kw.setdefault("ledger", InMemoryInvocationLedger())
    return N8nWorkflowExecutor(
        base_url="http://127.0.0.1:5678", workflows=[ECHO, FAIL], token=_token,
        transport=httpx.MockTransport(recorder), **kw,
    )


def request(args: dict, *, task: uuid.UUID | None = None, scope: dict | None = None,
            user: uuid.UUID | None = None) -> ExecutionRequest:
    return ExecutionRequest.from_invocation(ToolInvocation(
        tool_id="automation.run_workflow", operation="run_workflow", arguments=args,
        user_id=user or uuid.UUID(int=1), task_id=task or uuid.UUID(int=2),
        platform=ExecutionPlatform.SERVER, resource_scope=scope,
    ))


async def test_runs_a_pinned_workflow_with_typed_inputs():
    rec = Recorder()
    result = await executor(rec).run(request({"workflow_id": "jarvis.echo", "inputs": {"message": "hi", "count": 2}}))
    assert json.loads(result.content) == {"workflow_id": "jarvis.echo", "result": {"echo": {"message": "hi", "count": 2}}}
    (sent,) = rec.requests
    assert str(sent.url) == "http://127.0.0.1:5678/webhook/jarvis-echo"
    assert sent.headers["X-Jarvis-Webhook-Token"] == TOKEN
    assert len(sent.headers[IDEMPOTENCY_HEADER]) == 64
    body = json.loads(sent.content)
    # No user identity is sent to n8n; only the task id for tracing.
    assert set(body) == {"workflow_id", "inputs", "jarvis"} and set(body["jarvis"]) == {"task_id"}


@pytest.mark.parametrize("args", [
    {"workflow_id": "jarvis.unknown", "inputs": {}},                       # not pinned
    {"workflow_id": "http://evil.example/webhook/x", "inputs": {}},        # a URL is not an id
    {"workflow_id": "jarvis.echo", "inputs": {"message": "hi", "cmd": "rm -rf /"}},  # unknown field
    {"workflow_id": "jarvis.echo", "inputs": {}},                          # missing required
    {"workflow_id": "jarvis.echo", "inputs": {"message": 5}},              # wrong type
    {"workflow_id": "jarvis.echo", "inputs": {"message": "hi", "count": True}},  # bool is not an integer
    {"workflow_id": "jarvis.echo", "inputs": {"message": "x" * 41}},       # too long
    {"workflow_id": "jarvis.echo", "inputs": "message=hi"},                # not an object
    {"workflow_id": "jarvis.echo", "inputs": {"message": "hi"}, "url": "http://x"},  # extra argument
])
async def test_invalid_requests_are_refused_before_anything_is_sent(args):
    rec = Recorder()
    with pytest.raises(ExecutionError) as exc:
        await executor(rec).run(request(args))
    assert exc.value.code is ExecutionErrorCode.INVALID_ARGUMENTS
    assert rec.requests == []


async def test_a_grant_narrowed_to_one_workflow_is_honoured():
    rec = Recorder()
    with pytest.raises(ExecutionError) as exc:
        await executor(rec).run(request({"workflow_id": "jarvis.fail", "inputs": {}}, scope={"workflow_id": "jarvis.echo"}))
    assert exc.value.code is ExecutionErrorCode.SANDBOX_VIOLATION
    assert rec.requests == []


async def test_a_duplicate_in_the_same_task_is_not_re_executed():
    rec = Recorder()
    ex = executor(rec)
    args = {"workflow_id": "jarvis.echo", "inputs": {"message": "once"}}
    first = await ex.run(request(args))
    second = await ex.run(request(args))
    assert len(rec.requests) == 1
    assert "not re-executed" in second.content and first.content in second.content
    # A different task is a different request.
    await ex.run(request(args, task=uuid.UUID(int=99)))
    assert len(rec.requests) == 2


async def test_concurrent_duplicates_share_one_execution():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return httpx.Response(200, json={"ok": True})

    ex = N8nWorkflowExecutor(base_url="http://127.0.0.1:5678", workflows=[ECHO], token=_token, transport=Slow(),
                             ledger=InMemoryInvocationLedger())
    args = {"workflow_id": "jarvis.echo", "inputs": {"message": "x"}}
    a = asyncio.create_task(ex.run(request(args)))
    await started.wait()
    b = asyncio.create_task(ex.run(request(args)))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(a, b)
    assert calls == 1


async def test_workflow_failure_is_an_observation_and_not_retried():
    rec = Recorder(lambda r: httpx.Response(500, json={"message": "boom"}))
    with pytest.raises(ExecutionError) as exc:
        await executor(rec).run(request({"workflow_id": "jarvis.fail", "inputs": {}}))
    assert exc.value.code is ExecutionErrorCode.WORKFLOW_FAILED
    assert len(rec.requests) == 1


async def test_only_a_connection_that_never_opened_is_retried_once():
    attempts = 0

    def handler(r):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("refused", request=r)
        return httpx.Response(200, json={"ok": True})

    result = await executor(Recorder(handler)).run(request({"workflow_id": "jarvis.fail", "inputs": {}}))
    assert attempts == 2 and json.loads(result.content)["result"] == {"ok": True}

    def always_down(r):
        raise httpx.ConnectError("refused", request=r)

    rec = Recorder(always_down)
    with pytest.raises(ExecutionError) as exc:
        await executor(rec).run(request({"workflow_id": "jarvis.fail", "inputs": {}}))
    assert exc.value.code is ExecutionErrorCode.AUTOMATION_UNAVAILABLE and len(rec.requests) == 2


async def test_a_timeout_is_never_retried():
    rec = Recorder(lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r)))
    with pytest.raises(ExecutionError) as exc:
        await executor(rec).run(request({"workflow_id": "jarvis.fail", "inputs": {}}))
    assert exc.value.code is ExecutionErrorCode.TIMEOUT and len(rec.requests) == 1


@pytest.mark.parametrize("response, code", [
    (httpx.Response(302, headers={"location": "http://evil.example/"}), ExecutionErrorCode.AUTOMATION_UNAVAILABLE),
    (httpx.Response(403, json={"message": "Authorization data is wrong!"}), ExecutionErrorCode.AUTOMATION_UNAVAILABLE),
    (httpx.Response(404, json={"message": "webhook not registered"}), ExecutionErrorCode.AUTOMATION_UNAVAILABLE),
    (httpx.Response(200, text="<html>not json</html>"), ExecutionErrorCode.AUTOMATION_UNAVAILABLE),
    (httpx.Response(200, json=[1, 2]), ExecutionErrorCode.AUTOMATION_UNAVAILABLE),
    (httpx.Response(200, json={"big": "x" * 70000}), ExecutionErrorCode.RESPONSE_TOO_LARGE),
])
async def test_answers_outside_the_contract_are_refused(response, code):
    rec = Recorder(lambda r: response)
    with pytest.raises(ExecutionError) as exc:
        await executor(rec).run(request({"workflow_id": "jarvis.fail", "inputs": {}}))
    assert exc.value.code is code
    assert len(rec.requests) == 1  # a redirect is never followed


async def test_the_token_never_appears_in_a_result_or_an_error():
    def handler(r):
        return httpx.Response(200, json={"seen": r.headers.get("X-Jarvis-Webhook-Token", "")[:4]})

    ok = await executor(Recorder(handler)).run(request({"workflow_id": "jarvis.fail", "inputs": {}}))
    assert TOKEN not in ok.content
    for response in (httpx.Response(500, text=TOKEN), httpx.Response(200, text=TOKEN)):
        with pytest.raises(ExecutionError) as exc:
            await executor(Recorder(lambda r, resp=response: resp)).run(
                request({"workflow_id": "jarvis.fail", "inputs": {}}, task=uuid.uuid4()))
        assert TOKEN not in str(exc.value) and TOKEN not in exc.value.message


async def test_an_unavailable_credential_fails_closed_without_detail():
    async def broken() -> str:
        raise RuntimeError("secretstore:abcd internal detail")

    rec = Recorder()
    ex = N8nWorkflowExecutor(base_url="http://127.0.0.1:5678", workflows=[ECHO], token=broken,
                             transport=httpx.MockTransport(rec), ledger=InMemoryInvocationLedger())
    with pytest.raises(ExecutionError) as exc:
        await ex.run(request({"workflow_id": "jarvis.echo", "inputs": {"message": "x"}}))
    assert exc.value.code is ExecutionErrorCode.AUTOMATION_UNAVAILABLE
    assert "secretstore" not in exc.value.message and rec.requests == []


# ── capability, registry, configuration ───────────────────────────────────


def test_the_capability_is_closed_consequential_and_scopable_by_workflow():
    definition = lookup("automation.workflow")
    assert dict(definition.operations) == {"run_workflow": RiskCategory.CONSEQUENTIAL}
    assert definition.scope_keys == frozenset({"workflow_id"})


def test_the_tool_passes_registry_validation():
    registry = ToolRegistry()
    handle = registry.register(automation_workflow_tool(executor(Recorder())), enabled=True)
    assert handle.tool_id == "automation.run_workflow"


@pytest.mark.parametrize("payload, message", [
    ({"enabled": True, "workflows": [{"id": "a.b", "webhook_path": "p"}]}, "secret_ref"),
    ({"enabled": True, "secret_ref": "env:X"}, "at least one pinned workflow"),
    ({"base_url": "http://192.168.1.5:5678"}, "loopback"),
    ({"base_url": "https://n8n.example.com"}, "loopback"),
    ({"base_url": "http://127.0.0.1:5678/webhook/x"}, "bare"),
    ({"workflows": [{"id": "Bad Id", "webhook_path": "p"}]}, "workflow id"),
    ({"workflows": [{"id": "a.b", "webhook_path": "../admin"}]}, "one path segment"),
    ({"workflows": [{"id": "a.b", "webhook_path": "p"}, {"id": "a.b", "webhook_path": "q"}]}, "unique"),
    ({"secret_ref": "literal-token-value"}, "secret|reference|ref"),
])
def test_configuration_is_refused_when_unsafe(payload, message):
    with pytest.raises(ValueError, match=message):
        N8nConfig.model_validate(payload)


def test_automation_is_off_by_default_and_adds_no_tool():
    from server.composition.execution_tools import build_automation_executor

    from tests.runtime.conftest import base_config_payload

    config = AppConfig.model_validate(base_config_payload())
    assert config.automation.n8n.enabled is False
    assert build_automation_executor(config) is None


async def test_a_failure_before_anything_was_sent_is_not_remembered():
    """n8n down (the connection never opened): a later attempt in the same task
    must be allowed to run once n8n is back — it was never a run."""
    up = False

    def handler(r):
        if not up:
            raise httpx.ConnectError("refused", request=r)
        return httpx.Response(200, json={"ok": True})

    rec = Recorder(handler)
    ex = executor(rec)
    args = {"workflow_id": "jarvis.echo", "inputs": {"message": "retry later"}}
    with pytest.raises(ExecutionError) as exc:
        await ex.run(request(args))
    assert exc.value.code is ExecutionErrorCode.AUTOMATION_UNAVAILABLE
    up = True
    result = await ex.run(request(args))
    assert "not re-executed" not in result.content
    assert json.loads(result.content)["result"] == {"ok": True}


async def test_a_timeout_is_remembered_because_the_run_may_have_happened():
    rec = Recorder(lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r)))
    ex = executor(rec)
    args = {"workflow_id": "jarvis.fail", "inputs": {}}
    with pytest.raises(ExecutionError) as first:
        await ex.run(request(args))
    assert first.value.code is ExecutionErrorCode.TIMEOUT
    with pytest.raises(ExecutionError) as exc:
        await ex.run(request(args))
    # The outcome is unknown: refused as such, never re-sent.
    assert exc.value.code is ExecutionErrorCode.OUTCOME_UNCERTAIN
    assert "not repeated automatically" in exc.value.message and len(rec.requests) == 1


async def test_an_unexpected_error_never_leaves_a_concurrent_duplicate_waiting():
    started = asyncio.Event()
    release = asyncio.Event()

    class Broken(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req):
            started.set()
            await release.wait()
            raise RuntimeError("transport bug")

    ex = N8nWorkflowExecutor(base_url="http://127.0.0.1:5678", workflows=[ECHO], token=_token, transport=Broken(),
                             ledger=InMemoryInvocationLedger())
    args = {"workflow_id": "jarvis.echo", "inputs": {"message": "x"}}
    first = asyncio.create_task(ex.run(request(args)))
    await started.wait()
    second = asyncio.create_task(ex.run(request(args)))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), timeout=5)
    assert all(isinstance(r, Exception) for r in results)
