"""Platform adapters — the concrete `ToolAdapter` implementations this
branch ships (07 §1's per-family adapters; `docs/CAPABILITY_MATRIX.md`'s
"Code of record: ... `server/tools/platforms.py` (platform adapters)").

Every adapter here does exactly one thing: build an `ExecutionRequest` from
an already-authorized `ToolInvocation` (`04`/`07` ran before
`ToolRegistry.run` ever calls `execute()` — see `server/tools/registry.py`),
call the matching execution-layer primitive (`server.fs` / `server.net` /
`server.execution.process` / `server.execution.android`), and translate its
`ExecutionResult`/`ExecutionError` back into a `ToolOutput`. No adapter here
re-checks capability, visibility, ownership, or risk tier — doing so would
make this module the second authorization system the task brief explicitly
forbids.

**Addressing scope, stated explicitly.** `file.read`/`file.write`'s
`resource_scope` key is `sandbox_root` (a label — see `server/fs/__init__.py`
for why it is never treated as a path) and files inside it are addressed by
`arguments["relative_path"]`, never by a `FileResource` row's `resource_ref`.
Wiring individual `FileResource` visibility (RAUTH-003 inside a shared
graph's sandbox) into physical reads needs a DB session no `ToolAdapter.
execute` receives; that is `11`/a future branch's integration work, not
silently invented here. Every operation these adapters expose is therefore
authorized as a resource-less `TOOL_ACTION` (capability + tier +
`resource_scope`, already sufficient for FS-T5/FS-T6's cross-*user*
isolation, which `server.fs`'s root derivation enforces structurally
regardless).
"""

from __future__ import annotations

from typing import Mapping
from uuid import UUID

from server.execution.android import DeviceTransport, UnavailableDeviceTransport, build_operation
from server.execution.process import ConstrainedProcessExecutor
from server.fs import FilesystemSandbox
from server.net import EgressClient
from server.tools.registry import ToolDefinition
from shared.schemas.agent import ExecutionPlatform, OperationSpec, ToolInvocation, ToolOutput
from shared.schemas.agent_config import ToolContract
from shared.schemas.authorization import Operation, ResourceType
from shared.schemas.enums import RiskCategory
from shared.schemas.execution import EgressPolicy, ExecutionError, ExecutionErrorCode, ExecutionRequest, ExecutionResult

_ACTION = ResourceType.TOOL_ACTION.value
_CREATE = Operation.CREATE.value


def _tool_action(*names: str) -> dict[str, OperationSpec]:
    return {name: OperationSpec(_ACTION, _CREATE, requires_resource_ref=False) for name in names}


def _ok(result: ExecutionResult) -> ToolOutput:
    return ToolOutput(ok=True, content=result.content, units=result.units)


def _failed(exc: ExecutionError) -> ToolOutput:
    return ToolOutput(ok=False, error=exc.code.value)


def _string_argument(request: ExecutionRequest, key: str, *, default: str | None = None) -> str:
    value = request.arguments.get(key, default)
    if not isinstance(value, str) or not value:
        raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, f"{key!r} argument is required")
    return value


def _contract(
    tool_id: str, capability: str, risk: RiskCategory, confirm: bool, *,
    description: str, filesystem: dict | None = None, network: dict | None = None,
    timeout_seconds: int = 30,
) -> ToolContract:
    return ToolContract(
        tool_id=tool_id, version="1", description=description,
        input_schema={"type": "object"}, output_schema={"type": "string"},
        required_capability=capability, filesystem=filesystem or {}, network=network or {},
        risk_category=risk, timeout_seconds=timeout_seconds, confirmation_required=confirm,
        failure_behavior="observation", audit="every invocation",
    )


# ── filesystem (09) ──────────────────────────────────────────────────────


class FileReadAdapter:
    """`file.read`'s three automatic (low_read) operations."""

    def __init__(self, sandbox: FilesystemSandbox) -> None:
        self._sandbox = sandbox

    async def execute(self, invocation: ToolInvocation) -> ToolOutput:
        request = ExecutionRequest.from_invocation(invocation)
        try:
            root = self._sandbox.root_for(
                user_id=request.user_id, graph_id=None, label=request.scope.require("sandbox_root")
            )
            relative_path = request.arguments.get("relative_path", ".")
            if not isinstance(relative_path, str):
                raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, "relative_path must be a string")
            if request.operation == "read_file":
                result = self._sandbox.read_file(root, relative_path)
            elif request.operation == "list_directory":
                result = self._sandbox.list_directory(root, relative_path)
            elif request.operation == "stat":
                result = self._sandbox.stat(root, relative_path)
            else:
                raise ExecutionError(
                    ExecutionErrorCode.PLATFORM_UNSUPPORTED, f"unsupported operation {request.operation!r}"
                )
        except ExecutionError as exc:
            return _failed(exc)
        return _ok(result)


class FileWriteAdapter:
    """`file.write`'s four operations, spanning low_write through
    high_irreversible — the tier table (not this adapter) decides which of
    them pause for confirmation before `execute` is ever called."""

    def __init__(self, sandbox: FilesystemSandbox) -> None:
        self._sandbox = sandbox

    async def execute(self, invocation: ToolInvocation) -> ToolOutput:
        request = ExecutionRequest.from_invocation(invocation)
        try:
            root = self._sandbox.root_for(
                user_id=request.user_id, graph_id=None, label=request.scope.require("sandbox_root")
            )
            relative_path = _string_argument(request, "relative_path")
            if request.operation == "create_file":
                result = self._sandbox.create_file(root, relative_path, request.arguments.get("content", ""))
            elif request.operation == "write_file":
                result = self._sandbox.write_file(root, relative_path, request.arguments.get("content", ""))
            elif request.operation == "delete_file":
                result = self._sandbox.delete_file(root, relative_path)
            elif request.operation == "bulk_delete":
                result = self._sandbox.bulk_delete(root, relative_path)
            else:
                raise ExecutionError(
                    ExecutionErrorCode.PLATFORM_UNSUPPORTED, f"unsupported operation {request.operation!r}"
                )
        except ExecutionError as exc:
            return _failed(exc)
        return _ok(result)


def file_read_tool(sandbox: FilesystemSandbox) -> ToolDefinition:
    contract = _contract(
        "files.read", "file.read", RiskCategory.LOW_READ, False,
        description="Read files/directories inside a granted sandbox (09).",
        filesystem={"roots": True},
    )
    adapter = FileReadAdapter(sandbox)
    return ToolDefinition(
        contract=contract,
        operations=_tool_action("read_file", "list_directory", "stat"),
        adapters={ExecutionPlatform.SERVER: adapter},
    )


def file_write_tool(sandbox: FilesystemSandbox) -> ToolDefinition:
    contract = _contract(
        "files.write", "file.write", RiskCategory.HIGH_IRREVERSIBLE, True,
        description="Create/modify/delete files inside a granted sandbox (09).",
        filesystem={"roots": True},
    )
    adapter = FileWriteAdapter(sandbox)
    return ToolDefinition(
        contract=contract,
        operations=_tool_action("create_file", "write_file", "delete_file", "bulk_delete"),
        adapters={ExecutionPlatform.SERVER: adapter},
    )


# ── network egress (10) ──────────────────────────────────────────────────


class NetRequestAdapter:
    """`net.request`'s `get`/`post` operations, against one fixed
    `EgressPolicy` resolved once at construction from operator config
    (`ExecutionConfig.network.default_*`) — never from agent-supplied
    arguments, which would let a proposal widen its own destination list."""

    def __init__(self, client: EgressClient, *, egress_policy: EgressPolicy) -> None:
        self._client = client
        self._policy = egress_policy

    async def execute(self, invocation: ToolInvocation) -> ToolOutput:
        request = ExecutionRequest.from_invocation(invocation)
        try:
            url = _string_argument(request, "url")
            if request.operation == "get":
                result = await self._client.arequest(url, egress_policy=self._policy, method="GET")
            elif request.operation == "post":
                body_text = request.arguments.get("body", "")
                body_bytes = body_text.encode("utf-8") if isinstance(body_text, str) else b""
                result = await self._client.arequest(
                    url, egress_policy=self._policy, method="POST", body=body_bytes
                )
            else:
                raise ExecutionError(
                    ExecutionErrorCode.PLATFORM_UNSUPPORTED, f"unsupported operation {request.operation!r}"
                )
        except ExecutionError as exc:
            return _failed(exc)
        return _ok(result)


def net_request_tool(client: EgressClient, *, egress_policy: EgressPolicy) -> ToolDefinition:
    contract = _contract(
        "net.request", "net.request", RiskCategory.CONSEQUENTIAL, True,
        description="Make an outbound HTTP(S) request through the egress boundary (10).",
        network={"internet": egress_policy.internet, "destinations": sorted(egress_policy.destinations)},
    )
    adapter = NetRequestAdapter(client, egress_policy=egress_policy)
    return ToolDefinition(
        contract=contract,
        operations=_tool_action("get", "post"),
        adapters={ExecutionPlatform.SERVER: adapter},
    )


# ── process execution (system.restricted, 08 §6) ────────────────────────


class ShellCommandAdapter:
    """The one, deliberately isolated `system.restricted` operation.
    `argv`/`cwd`/`env_overrides`/`timeout` all pass straight to
    `ConstrainedProcessExecutor`, which does the actual allow-listing,
    sanitization, and bounding — this adapter adds no policy of its own."""

    def __init__(self, executor: ConstrainedProcessExecutor, *, sandbox: FilesystemSandbox) -> None:
        self._executor = executor
        self._sandbox = sandbox

    def release_task(self, task_id: UUID) -> None:
        """09 §7: the task's temp directory — the only place a process may
        write — is removed when the task ends."""

        self._sandbox.cleanup_task(task_id=task_id)

    async def execute(self, invocation: ToolInvocation) -> ToolOutput:
        request = ExecutionRequest.from_invocation(invocation)
        try:
            argv = request.arguments.get("argv")
            if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
                raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, "argv must be a list of strings")
            # The process always runs inside this task's own sandbox temp
            # root — never a path taken from arguments — so a shell command
            # gets a real, already-contained working directory without this
            # adapter having to validate one itself (09 §1's derive-never-
            # accept-raw rule, applied to a cwd instead of a file path).
            cwd = self._sandbox.task_temp(task_id=request.task_id)
            env_overrides = request.arguments.get("env_overrides") or {}
            if not isinstance(env_overrides, dict):
                raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, "env_overrides must be an object")
            timeout = request.arguments.get("timeout")
            result = await self._executor.run(
                argv, cwd=cwd, env_overrides=env_overrides,
                timeout=float(timeout) if timeout is not None else None,
            )
        except ExecutionError as exc:
            return _failed(exc)
        return _ok(result)


def shell_command_tool(executor: ConstrainedProcessExecutor, *, sandbox: FilesystemSandbox) -> ToolDefinition:
    contract = _contract(
        "system.shell", "system.restricted", RiskCategory.HIGH_IRREVERSIBLE, True,
        description="Run an allow-listed executable under strict resource limits (08 §6).",
        timeout_seconds=60,
    )
    adapter = ShellCommandAdapter(executor, sandbox=sandbox)
    return ToolDefinition(
        contract=contract,
        operations=_tool_action("run_shell_command"),
        adapters={ExecutionPlatform.SERVER: adapter, ExecutionPlatform.LINUX: adapter},
    )


# ── Android/Shizuku (08) ─────────────────────────────────────────────────


class AndroidDeviceAdapter:
    """One adapter shared by every device-family tool: it only differs by
    which capability it was constructed for, since `build_operation` already
    encodes the enumerated operation→primitive mapping per capability."""

    def __init__(self, capability: str, transport: DeviceTransport) -> None:
        self._capability = capability
        self._transport = transport

    async def execute(self, invocation: ToolInvocation) -> ToolOutput:
        request = ExecutionRequest.from_invocation(invocation)
        try:
            package_name = request.scope.get("package_name")
            operation = build_operation(
                capability=self._capability, operation=request.operation,
                package_name=package_name, arguments=request.arguments,
                user_id=request.user_id, task_id=request.task_id, device_id=request.device_id,
            )
            result = await self._transport.send(operation)
        except ExecutionError as exc:
            return _failed(exc)
        return _ok(result)


def _device_transport_or_default(transport: DeviceTransport | None) -> DeviceTransport:
    return transport if transport is not None else UnavailableDeviceTransport()


def android_app_interact_tool(transport: DeviceTransport | None = None) -> ToolDefinition:
    adapter = AndroidDeviceAdapter("app.interact", _device_transport_or_default(transport))
    contract = _contract(
        "device.app_interact", "app.interact", RiskCategory.CONSEQUENTIAL, True,
        description="Compose UI interactions within a named app (08 §2).",
    )
    return ToolDefinition(
        contract=contract,
        operations=_tool_action("tap", "swipe", "input_text", "read_screen_element", "launch_activity"),
        adapters={ExecutionPlatform.ANDROID: adapter},
    )


def android_device_read_tool(transport: DeviceTransport | None = None) -> ToolDefinition:
    adapter = AndroidDeviceAdapter("device.read", _device_transport_or_default(transport))
    contract = _contract(
        "device.read", "device.read", RiskCategory.LOW_READ, False,
        description="Read device state the user has exposed (08 §2).",
    )
    return ToolDefinition(
        contract=contract,
        operations=_tool_action("read_screen", "read_battery", "read_notification"),
        adapters={ExecutionPlatform.ANDROID: adapter},
    )


__all__ = [
    "AndroidDeviceAdapter",
    "FileReadAdapter",
    "FileWriteAdapter",
    "NetRequestAdapter",
    "ShellCommandAdapter",
    "android_app_interact_tool",
    "android_device_read_tool",
    "file_read_tool",
    "file_write_tool",
    "net_request_tool",
    "shell_command_tool",
]
