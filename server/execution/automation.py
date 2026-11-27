"""n8n as an execution target (docs/32_AUTOMATION_N8N.md, [PROPOSAL] OD-AUTO-1).

    agent proposes → Track B authorizes (capability, grant, scope, tier,
    confirmation) → **this module executes** → n8n runs the pinned workflow

This module is execution, not authority. It receives only an
`ExecutionRequest` built from an already-authorized `ToolInvocation`; nothing
here decides whether the user may run a workflow, and nothing n8n returns can
change that decision. What it does enforce is the *shape* the operator pinned:

* the workflow id must be one of the operator's pinned workflows — a model
  cannot name a webhook path, a URL, a method or a header;
* the inputs must be exactly that workflow's typed fields (unknown fields,
  missing required fields, wrong types, over-long strings are refused before
  anything is sent);
* a grant narrowed to one workflow (`resource_scope.workflow_id`) is honoured
  here too, as a second layer behind the engine;
* the request body is size-capped, redirects are never followed, and the
  response must be one bounded JSON object;
* **duplicate prevention**: the idempotency key is derived from the user, the
  task, the workflow and the canonical inputs. A repeat of a call that may have
  reached n8n (success, workflow failure, timeout, refusal) returns the
  recorded outcome without calling n8n again, and a concurrent repeat waits for
  the first. A failure before anything was sent is not remembered, so a later
  attempt may run. The key is also sent to n8n
  (`X-Jarvis-Idempotency-Key`) so a workflow can dedupe on its own side;
* **controlled retry**: one retry, and only when the connection was never
  established (the request cannot have reached n8n). A timeout or any answer
  is never retried, because a webhook POST is not idempotent.

The webhook token is resolved by the composition root and handed in as an
opaque async callable; this package never imports `server.secrets` (import
contract "Execution/fs/net never import secrets"). The token is sent only in
the configured header to the loopback origin, and never appears in a result,
an error message or a log line.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

import httpx

from shared.schemas.execution import (
    ExecutionError,
    ExecutionErrorCode,
    ExecutionRequest,
    ExecutionResult,
)

logger = logging.getLogger("hypermind.execution.automation")

IDEMPOTENCY_HEADER = "X-Jarvis-Idempotency-Key"
_ALLOWED_ARGUMENTS = frozenset({"workflow_id", "inputs"})
_TYPES: Mapping[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
}

TokenSource = Callable[[], Awaitable[str]]


@dataclass(frozen=True)
class InputField:
    type: str
    required: bool = True
    max_length: int = 500


@dataclass(frozen=True)
class PinnedWorkflow:
    id: str
    webhook_path: str
    description: str
    inputs: Mapping[str, InputField]
    timeout_seconds: float = 20.0


@dataclass(frozen=True)
class _NotSent:
    """A first attempt that failed before anything reached n8n."""

    error: ExecutionError


def _not_sent(error: ExecutionError) -> ExecutionError:
    error.not_sent = True  # type: ignore[attr-defined]
    return error


def _invalid(message: str) -> ExecutionError:
    return ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, message)


def validate_inputs(workflow: PinnedWorkflow, inputs: Any) -> dict[str, Any]:
    """Exactly the pinned fields, with the pinned types. Never coerces."""

    if not isinstance(inputs, dict):
        raise _invalid("inputs must be an object")
    unknown = set(inputs) - set(workflow.inputs)
    if unknown:
        raise _invalid(f"unknown input field(s) for {workflow.id}: {sorted(unknown)}")
    clean: dict[str, Any] = {}
    for name, field in workflow.inputs.items():
        if name not in inputs:
            if field.required:
                raise _invalid(f"missing required input {name!r} for {workflow.id}")
            continue
        value = inputs[name]
        expected = _TYPES[field.type]
        # bool is an int in Python; an integer/number field never accepts one.
        if isinstance(value, bool) and field.type != "boolean":
            raise _invalid(f"input {name!r} must be {field.type}")
        if not isinstance(value, expected):
            raise _invalid(f"input {name!r} must be {field.type}")
        if field.type == "string" and len(value) > field.max_length:
            raise _invalid(f"input {name!r} is longer than {field.max_length} characters")
        clean[name] = value
    return clean


def idempotency_key(request: ExecutionRequest, workflow_id: str, inputs: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {"u": str(request.user_id), "t": str(request.task_id), "w": workflow_id, "i": inputs},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class N8nWorkflowExecutor:
    """Runs pinned n8n workflows through their production webhooks."""

    def __init__(
        self,
        *,
        base_url: str,
        workflows: list[PinnedWorkflow],
        token: TokenSource,
        auth_header: str = "X-Jarvis-Webhook-Token",
        max_request_bytes: int = 8192,
        max_response_bytes: int = 65536,
        transport: httpx.AsyncBaseTransport | None = None,
        max_remembered: int = 1024,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._workflows = {w.id: w for w in workflows}
        self._token = token
        self._auth_header = auth_header
        self._max_request = max_request_bytes
        self._max_response = max_response_bytes
        self._transport = transport
        self._done: OrderedDict[str, ExecutionResult | ExecutionError] = OrderedDict()
        self._inflight: dict[str, asyncio.Future] = {}
        self._max_remembered = max_remembered

    @property
    def workflows(self) -> Mapping[str, PinnedWorkflow]:
        return dict(self._workflows)

    async def run(self, request: ExecutionRequest) -> ExecutionResult:
        unknown = set(request.arguments) - _ALLOWED_ARGUMENTS
        if unknown:
            raise _invalid(f"unknown argument(s): {sorted(unknown)}")
        workflow_id = request.arguments.get("workflow_id")
        if not isinstance(workflow_id, str) or workflow_id not in self._workflows:
            # Never echo a model-supplied id back as if it were a workflow.
            raise _invalid("workflow_id is not one of the operator's pinned workflows")
        scoped = request.scope.raw.get("workflow_id") if request.scope.raw else None
        if scoped is not None and scoped != workflow_id:
            raise ExecutionError(
                ExecutionErrorCode.SANDBOX_VIOLATION, "the grant is limited to a different workflow"
            )
        workflow = self._workflows[workflow_id]
        inputs = validate_inputs(workflow, request.arguments.get("inputs", {}))
        key = idempotency_key(request, workflow_id, inputs)

        if key in self._done:
            return self._replay(self._done[key])
        pending = self._inflight.get(key)
        if pending is not None:
            return self._replay(await asyncio.shield(pending))

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            outcome: ExecutionResult | ExecutionError
            try:
                outcome = await self._call(request, workflow, inputs, key)
            except ExecutionError as exc:
                outcome = exc
            except BaseException:
                # Anything unexpected (or cancellation): never leave a concurrent
                # duplicate waiting forever, and remember nothing.
                outcome = ExecutionError(ExecutionErrorCode.INTERNAL, "automation call failed")
                future.set_result(_NotSent(outcome))
                raise
            # Remember only outcomes where the request may have reached n8n. A
            # failure before anything was sent (credential unavailable, the
            # connection never opened, request too large) is not a run, so a
            # later attempt must be allowed to execute.
            if isinstance(outcome, ExecutionError) and getattr(outcome, "not_sent", False):
                future.set_result(_NotSent(outcome))
            else:
                self._remember(key, outcome)
                future.set_result(outcome)
        finally:
            self._inflight.pop(key, None)
        if isinstance(outcome, ExecutionError):
            raise outcome
        return outcome

    def _replay(self, outcome: "ExecutionResult | ExecutionError | _NotSent") -> ExecutionResult:
        if isinstance(outcome, _NotSent):
            # The first attempt never reached n8n: report its failure, unchanged.
            raise ExecutionError(outcome.error.code, outcome.error.message)
        if isinstance(outcome, ExecutionError):
            raise ExecutionError(outcome.code, f"{outcome.message} (duplicate request: not re-executed)")
        return ExecutionResult(content=outcome.content + "\n[duplicate request: recorded result, not re-executed]")

    def _remember(self, key: str, outcome: ExecutionResult | ExecutionError) -> None:
        self._done[key] = outcome
        self._done.move_to_end(key)
        while len(self._done) > self._max_remembered:
            self._done.popitem(last=False)

    async def _call(
        self, request: ExecutionRequest, workflow: PinnedWorkflow, inputs: dict[str, Any], key: str
    ) -> ExecutionResult:
        body = json.dumps(
            {"workflow_id": workflow.id, "inputs": inputs, "jarvis": {"task_id": str(request.task_id)}},
            separators=(",", ":"),
        ).encode()
        if len(body) > self._max_request:
            raise _not_sent(ExecutionError(ExecutionErrorCode.RESOURCE_EXHAUSTED, "workflow request is too large"))
        try:
            token = await self._token()
        except Exception:  # noqa: BLE001 — never let a secret-store detail escape
            raise _not_sent(
                ExecutionError(ExecutionErrorCode.AUTOMATION_UNAVAILABLE, "automation credential unavailable")
            ) from None
        headers = {
            "Content-Type": "application/json",
            self._auth_header: token,
            IDEMPOTENCY_HEADER: key,
        }
        url = f"{self._base}/webhook/{workflow.webhook_path}"
        timeout = httpx.Timeout(workflow.timeout_seconds, connect=min(5.0, workflow.timeout_seconds))
        async with httpx.AsyncClient(
            transport=self._transport, timeout=timeout, follow_redirects=False, trust_env=False
        ) as client:
            response = None
            for attempt in (1, 2):
                try:
                    response = await client.post(url, content=body, headers=headers)
                    break
                except httpx.ConnectError:
                    # Nothing reached n8n: the one safe retry.
                    if attempt == 2:
                        raise _not_sent(ExecutionError(
                            ExecutionErrorCode.AUTOMATION_UNAVAILABLE, "automation host unreachable"
                        )) from None
                except httpx.TimeoutException:
                    raise ExecutionError(ExecutionErrorCode.TIMEOUT, "workflow did not answer in time") from None
                except httpx.HTTPError:
                    raise ExecutionError(
                        ExecutionErrorCode.AUTOMATION_UNAVAILABLE, "automation host error"
                    ) from None
            assert response is not None
            raw = response.content
        logger.info("automation workflow=%s status=%s bytes=%s", workflow.id, response.status_code, len(raw))
        if len(raw) > self._max_response:
            raise ExecutionError(ExecutionErrorCode.RESPONSE_TOO_LARGE, "workflow response is too large")
        if response.is_redirect or response.status_code in (401, 403, 404):
            # Auth refused, workflow inactive/absent, or a redirect: the
            # contract is broken, not the workflow.
            raise ExecutionError(ExecutionErrorCode.AUTOMATION_UNAVAILABLE, f"automation host refused ({response.status_code})")
        if response.status_code >= 400:
            raise ExecutionError(ExecutionErrorCode.WORKFLOW_FAILED, f"workflow reported failure ({response.status_code})")
        try:
            payload = json.loads(raw)
        except ValueError:
            raise ExecutionError(ExecutionErrorCode.AUTOMATION_UNAVAILABLE, "workflow answered with non-JSON") from None
        if isinstance(payload, list) and len(payload) == 1:
            payload = payload[0]
        if not isinstance(payload, dict):
            raise ExecutionError(ExecutionErrorCode.AUTOMATION_UNAVAILABLE, "workflow answer is not one JSON object")
        content = json.dumps({"workflow_id": workflow.id, "result": payload}, sort_keys=True, ensure_ascii=False)
        return ExecutionResult(content=content)


__all__ = [
    "IDEMPOTENCY_HEADER",
    "InputField",
    "N8nWorkflowExecutor",
    "PinnedWorkflow",
    "idempotency_key",
    "validate_inputs",
]
