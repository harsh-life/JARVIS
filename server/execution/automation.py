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
  response is read as a stream and refused **as soon as** it crosses
  `max_response_bytes` (uncompressed only — `Accept-Encoding: identity`; a
  declared oversize is refused before reading) — it is never buffered whole;
  it must be one JSON object;
* **durable duplicate prevention**: the idempotency key is derived from the
  user, the task, the workflow and the canonical inputs, and recorded in a
  durable ledger (`automation_invocations`, `InvocationLedger`) **before**
  anything is sent. A key that may have reached n8n is never executed again:
  a completed call replays its recorded result, a failed one its failure, and
  one whose outcome is unknown (timeout, lost connection, cancellation, a
  crash mid-call, or another attempt holding it) is refused as
  `outcome_uncertain` — never retried automatically. Only a call that
  provably sent nothing (`failed_before_send`) may be attempted again. The key
  is also sent to n8n (`X-Jarvis-Idempotency-Key`) so a workflow can dedupe on
  its own side;
* **exactly-once is not claimed**: JARVIS can guarantee it never *re-sends*
  a key, not that n8n (or the systems a workflow calls) performed an action
  exactly once. That needs the downstream workflow to dedupe on the key;
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
import contextlib
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Protocol

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


class InvocationState(str, Enum):
    """The durable record's states (`automation_invocations.state`)."""

    RESERVED = "reserved"                      # claimed; nothing sent yet
    IN_PROGRESS = "in_progress"                # committed just before the POST
    COMPLETED = "completed"                    # n8n answered with a result
    FAILED = "failed"                          # n8n answered with a failure (or an unusable answer)
    FAILED_BEFORE_SEND = "failed_before_send"  # nothing reached n8n: may be attempted again
    OUTCOME_UNCERTAIN = "outcome_uncertain"    # sent, no usable answer: never repeated automatically


@dataclass(frozen=True)
class LedgerEntry:
    state: InvocationState
    content: str | None = None
    error_code: str | None = None


class InvocationLedger(Protocol):
    """Where the executor keeps its idempotency record. The production
    implementation is table-backed and durable (`server/composition/
    automation_ledger.py`); this package only states the contract."""

    async def reserve(self, key: str, *, user_id: uuid.UUID, task_id: uuid.UUID | None,
                      workflow_id: str) -> LedgerEntry | None:
        """Claim `key` for a new attempt, durably. `None`: the caller now holds
        it (a new key, a previous attempt that provably sent nothing, or a
        stale reservation whose holder never marked it in progress). Otherwise
        the existing entry, which the caller must never re-execute."""
        ...

    async def record(self, key: str, state: InvocationState, *, content: str | None = None,
                     error_code: str | None = None) -> None:
        """Move `key` to `state`, durably, before returning."""
        ...


class InMemoryInvocationLedger:
    """**Not durable** — process memory only, for unit tests. It does not
    survive a restart and must never back a deployment; the composition root
    always wires the table-backed ledger."""

    def __init__(self) -> None:
        self.entries: dict[str, LedgerEntry] = {}

    async def reserve(self, key, *, user_id, task_id, workflow_id):  # noqa: ARG002
        existing = self.entries.get(key)
        if existing is not None and existing.state is not InvocationState.FAILED_BEFORE_SEND:
            return existing
        self.entries[key] = LedgerEntry(InvocationState.RESERVED)
        return None

    async def record(self, key, state, *, content=None, error_code=None):
        self.entries[key] = LedgerEntry(state, content, error_code)


class _Sent(Exception):
    """Internal: the request may have reached n8n and no usable answer came back."""

    def __init__(self, error: ExecutionError) -> None:
        super().__init__(error.message)
        self.error = error


class N8nWorkflowExecutor:
    """Runs pinned n8n workflows through their production webhooks."""

    def __init__(
        self,
        *,
        base_url: str,
        workflows: list[PinnedWorkflow],
        token: TokenSource,
        ledger: InvocationLedger,
        auth_header: str = "X-Jarvis-Webhook-Token",
        max_request_bytes: int = 8192,
        max_response_bytes: int = 65536,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._workflows = {w.id: w for w in workflows}
        self._token = token
        self._ledger = ledger
        self._auth_header = auth_header
        self._max_request = max_request_bytes
        self._max_response = max_response_bytes
        self._transport = transport
        # In-process serialisation of concurrent duplicates (the ledger is what
        # holds across processes and restarts).
        self._inflight: dict[str, asyncio.Lock] = {}

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

        lock = self._inflight.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                return await self._run_once(request, workflow, inputs, key)
        finally:
            if not lock.locked():
                self._inflight.pop(key, None)

    async def _run_once(self, request: ExecutionRequest, workflow: PinnedWorkflow,
                        inputs: dict[str, Any], key: str) -> ExecutionResult:
        try:
            existing = await self._ledger.reserve(key, user_id=request.user_id, task_id=request.task_id,
                                                  workflow_id=workflow.id)
        except Exception:  # noqa: BLE001 — without a durable reservation, nothing is sent
            logger.exception("automation idempotency record unavailable; workflow=%s not sent", workflow.id)
            raise ExecutionError(ExecutionErrorCode.AUTOMATION_UNAVAILABLE,
                                 "automation idempotency record unavailable; nothing was sent") from None
        if existing is not None:
            return self._replay(existing)

        sent = False
        try:
            try:
                body, headers, url, timeout = await self._prepare(request, workflow, inputs, key)
            except ExecutionError as exc:
                await self._ledger.record(key, InvocationState.FAILED_BEFORE_SEND, error_code=exc.code.value)
                raise
            # Durable before anything leaves: from here on a crash, a timeout
            # or a cancellation is an uncertain outcome, never a retry.
            await self._ledger.record(key, InvocationState.IN_PROGRESS)
            sent = True
            try:
                result = await self._post(workflow, url, body, headers, timeout)
            except _NotSentError as exc:
                sent = False
                await self._ledger.record(key, InvocationState.FAILED_BEFORE_SEND, error_code=exc.error.code.value)
                raise exc.error from None
            except _Sent as exc:
                await self._ledger.record(key, InvocationState.OUTCOME_UNCERTAIN, error_code=exc.error.code.value)
                raise exc.error from None
            except ExecutionError as exc:
                await self._ledger.record(key, InvocationState.FAILED, error_code=exc.code.value)
                raise
            await self._ledger.record(key, InvocationState.COMPLETED, content=result.content)
            return result
        except BaseException as exc:
            if sent and not isinstance(exc, ExecutionError):
                # Cancelled or crashed after the request may have left: record
                # the uncertainty even while being cancelled.
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._ledger.record(
                        key, InvocationState.OUTCOME_UNCERTAIN, error_code=ExecutionErrorCode.CANCELLED.value
                        if isinstance(exc, asyncio.CancelledError) else ExecutionErrorCode.INTERNAL.value))
            raise

    @staticmethod
    def _replay(entry: LedgerEntry) -> ExecutionResult:
        if entry.state is InvocationState.COMPLETED:
            return ExecutionResult(
                content=(entry.content or "") + "\n[duplicate request: recorded result, not re-executed]")
        if entry.state is InvocationState.FAILED:
            code = ExecutionErrorCode(entry.error_code) if entry.error_code in _CODES else \
                ExecutionErrorCode.WORKFLOW_FAILED
            raise ExecutionError(code, "the identical request already failed (duplicate request: not re-executed)")
        # reserved / in_progress (another attempt holds it) or outcome_uncertain.
        raise ExecutionError(
            ExecutionErrorCode.OUTCOME_UNCERTAIN,
            "an identical request is in progress or may already have run; its outcome is not known, "
            "so it is not repeated automatically",
        )

    async def _prepare(self, request: ExecutionRequest, workflow: PinnedWorkflow, inputs: dict[str, Any],
                       key: str) -> tuple[bytes, dict[str, str], str, httpx.Timeout]:
        body = json.dumps(
            {"workflow_id": workflow.id, "inputs": inputs, "jarvis": {"task_id": str(request.task_id)}},
            separators=(",", ":"),
        ).encode()
        if len(body) > self._max_request:
            raise ExecutionError(ExecutionErrorCode.RESOURCE_EXHAUSTED, "workflow request is too large")
        try:
            token = await self._token()
        except Exception:  # noqa: BLE001 — never let a secret-store detail escape
            raise ExecutionError(ExecutionErrorCode.AUTOMATION_UNAVAILABLE, "automation credential unavailable") \
                from None
        headers = {
            "Content-Type": "application/json",
            # Uncompressed only: the size cap below counts exactly the bytes
            # received, and no decompression bomb can expand past it.
            "Accept-Encoding": "identity",
            self._auth_header: token,
            IDEMPOTENCY_HEADER: key,
        }
        url = f"{self._base}/webhook/{workflow.webhook_path}"
        timeout = httpx.Timeout(workflow.timeout_seconds, connect=min(5.0, workflow.timeout_seconds))
        return body, headers, url, timeout

    async def _post(self, workflow: PinnedWorkflow, url: str, body: bytes, headers: dict[str, str],
                    timeout: httpx.Timeout) -> ExecutionResult:
        async with httpx.AsyncClient(
            transport=self._transport, timeout=timeout, follow_redirects=False, trust_env=False
        ) as client:
            for attempt in (1, 2):
                try:
                    async with client.stream("POST", url, content=body, headers=headers) as response:
                        status, raw = response.status_code, await self._read_bounded(response)
                        is_redirect = response.is_redirect
                    break
                except httpx.ConnectError:
                    # Nothing reached n8n: the one safe retry.
                    if attempt == 2:
                        raise _NotSentError(ExecutionError(
                            ExecutionErrorCode.AUTOMATION_UNAVAILABLE, "automation host unreachable")) from None
                except httpx.TimeoutException:
                    raise _Sent(ExecutionError(ExecutionErrorCode.TIMEOUT,
                                               "workflow did not answer in time; outcome unknown")) from None
                except httpx.HTTPError:
                    raise _Sent(ExecutionError(ExecutionErrorCode.AUTOMATION_UNAVAILABLE,
                                               "automation host error; outcome unknown")) from None
        logger.info("automation workflow=%s status=%s bytes=%s", workflow.id, status, len(raw))
        if is_redirect or status in (401, 403, 404):
            # Auth refused, workflow inactive/absent, or a redirect: the
            # contract is broken, not the workflow.
            raise ExecutionError(ExecutionErrorCode.AUTOMATION_UNAVAILABLE, f"automation host refused ({status})")
        if status >= 400:
            raise ExecutionError(ExecutionErrorCode.WORKFLOW_FAILED, f"workflow reported failure ({status})")
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

    async def _read_bounded(self, response: httpx.Response) -> bytes:
        """At most `max_response_bytes`, counted while streaming: an oversized
        answer is refused as soon as it crosses the limit — never buffered
        whole. A declared length over the limit is refused before reading."""

        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in ("", "identity"):
            raise ExecutionError(ExecutionErrorCode.AUTOMATION_UNAVAILABLE,
                                 "workflow answered with an encoded body (identity was requested)")
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self._max_response:
            raise ExecutionError(ExecutionErrorCode.RESPONSE_TOO_LARGE, "workflow response is too large")
        received = bytearray()
        # Non-identity encodings were refused above, so these are the bytes received.
        async for chunk in response.aiter_bytes():
            if len(received) + len(chunk) > self._max_response:
                raise ExecutionError(ExecutionErrorCode.RESPONSE_TOO_LARGE, "workflow response is too large")
            received += chunk
        return bytes(received)


class _NotSentError(Exception):
    """Internal: provably nothing reached n8n (the connection never opened)."""

    def __init__(self, error: ExecutionError) -> None:
        super().__init__(error.message)
        self.error = error


_CODES = {code.value for code in ExecutionErrorCode}


__all__ = [
    "IDEMPOTENCY_HEADER",
    "InMemoryInvocationLedger",
    "InputField",
    "InvocationLedger",
    "InvocationState",
    "LedgerEntry",
    "N8nWorkflowExecutor",
    "PinnedWorkflow",
    "idempotency_key",
    "validate_inputs",
]
