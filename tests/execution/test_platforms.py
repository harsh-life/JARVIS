"""server/tools/platforms.py — the concrete ToolAdapter implementations.

Each adapter is exercised directly against a real ExecutionRequest-shaped
ToolInvocation and a real primitive (FilesystemSandbox / EgressClient /
ConstrainedProcessExecutor / a fake DeviceTransport) — no mocking of the
adapter under test itself, only of the one thing genuinely outside this
repository's reach (a connected Android device).
"""

from __future__ import annotations

import uuid

import pytest

from server.execution.android import DeviceOperation, UnavailableDeviceTransport
from server.execution.process import ConstrainedProcessExecutor
from server.fs import FilesystemSandbox
from server.net import EgressClient
from server.tools.platforms import (
    AndroidDeviceAdapter,
    FileReadAdapter,
    FileWriteAdapter,
    NetRequestAdapter,
    ShellCommandAdapter,
    android_app_interact_tool,
    file_read_tool,
    file_write_tool,
    net_request_tool,
    shell_command_tool,
)
from server.tools.registry import ToolRegistry
from shared.schemas.agent import ExecutionPlatform, ToolInvocation
from shared.schemas.execution import EgressPolicy
from tests.support import TEST_PROCESS_CONFINEMENT


def invocation(tool_id, operation, *, user_id=None, task_id=None, arguments=None, scope=None,
               device_id=None) -> ToolInvocation:
    return ToolInvocation(
        tool_id=tool_id, operation=operation, arguments=arguments or {},
        user_id=user_id or uuid.uuid4(), task_id=task_id or uuid.uuid4(),
        platform=ExecutionPlatform.SERVER, resource_scope=scope,
        device_id=device_id or uuid.uuid4(),
    )


# ── file adapters ─────────────────────────────────────────────────────────


@pytest.fixture
def fs(tmp_path) -> FilesystemSandbox:
    return FilesystemSandbox(base_root=str(tmp_path / "sandboxes"))


async def test_file_write_then_read_round_trips(fs):
    write_adapter = FileWriteAdapter(fs)
    read_adapter = FileReadAdapter(fs)
    user_id = uuid.uuid4()
    scope = {"sandbox_root": "notes"}

    out = await write_adapter.execute(invocation(
        "files.write", "create_file", user_id=user_id,
        arguments={"relative_path": "a.txt", "content": "hello"}, scope=scope,
    ))
    assert out.ok, out.error

    out = await read_adapter.execute(invocation(
        "files.read", "read_file", user_id=user_id,
        arguments={"relative_path": "a.txt"}, scope=scope,
    ))
    assert out.ok
    assert out.content == "hello"


async def test_file_read_missing_sandbox_root_scope_fails_closed(fs):
    read_adapter = FileReadAdapter(fs)
    out = await read_adapter.execute(invocation(
        "files.read", "read_file", arguments={"relative_path": "a.txt"}, scope=None,
    ))
    assert not out.ok
    assert out.error == "missing_execution_context"


async def test_file_read_traversal_attempt_fails_closed_via_the_adapter(fs):
    read_adapter = FileReadAdapter(fs)
    out = await read_adapter.execute(invocation(
        "files.read", "read_file", arguments={"relative_path": "../../etc/passwd"},
        scope={"sandbox_root": "notes"},
    ))
    assert not out.ok
    assert out.error == "forbidden_path"


async def test_different_users_get_isolated_sandboxes_through_the_adapter(fs):
    write_adapter = FileWriteAdapter(fs)
    read_adapter = FileReadAdapter(fs)
    alice, bob = uuid.uuid4(), uuid.uuid4()
    scope = {"sandbox_root": "notes"}

    await write_adapter.execute(invocation(
        "files.write", "create_file", user_id=alice,
        arguments={"relative_path": "secret.txt", "content": "alice's data"}, scope=scope,
    ))
    out = await read_adapter.execute(invocation(
        "files.read", "read_file", user_id=bob,
        arguments={"relative_path": "secret.txt"}, scope=scope,
    ))
    assert not out.ok  # bob's own sandbox never contains alice's file


async def test_file_read_unsupported_operation_is_platform_unsupported(fs):
    read_adapter = FileReadAdapter(fs)
    out = await read_adapter.execute(invocation(
        "files.read", "delete_everything", arguments={}, scope={"sandbox_root": "notes"},
    ))
    assert not out.ok
    assert out.error == "platform_unsupported"


def test_file_tools_register_successfully(fs):
    registry = ToolRegistry()
    registry.register(file_read_tool(fs), enabled=True)
    registry.register(file_write_tool(fs), enabled=True)
    assert registry.resolve("files.read") is not None
    assert registry.resolve("files.write") is not None


# ── net adapter ───────────────────────────────────────────────────────────


async def test_net_request_adapter_denies_by_default():
    adapter = NetRequestAdapter(EgressClient(), egress_policy=EgressPolicy())
    out = await adapter.execute(invocation("net.request", "get", arguments={"url": "http://example.com/"}))
    assert not out.ok
    assert out.error == "egress_denied"


async def test_net_request_adapter_requires_url_argument():
    policy = EgressPolicy(destinations=frozenset({"example.com"}))
    adapter = NetRequestAdapter(EgressClient(), egress_policy=policy)
    out = await adapter.execute(invocation("net.request", "get", arguments={}))
    assert not out.ok
    assert out.error == "invalid_arguments"


async def test_net_request_adapter_unsupported_operation():
    adapter = NetRequestAdapter(EgressClient(), egress_policy=EgressPolicy(internet=True))
    out = await adapter.execute(invocation("net.request", "delete", arguments={"url": "http://x/"}))
    assert not out.ok
    assert out.error == "platform_unsupported"


def test_net_request_tool_registers_successfully():
    registry = ToolRegistry()
    policy = EgressPolicy(destinations=frozenset({"example.com"}))
    registry.register(net_request_tool(EgressClient(), egress_policy=policy), enabled=True)
    assert registry.resolve("net.request") is not None


# ── process adapter ───────────────────────────────────────────────────────


@pytest.fixture
def fs_for_process(tmp_path) -> FilesystemSandbox:
    return FilesystemSandbox(base_root=str(tmp_path / "sandboxes"))


async def test_shell_adapter_runs_an_allowlisted_command(fs_for_process):
    executor = ConstrainedProcessExecutor(confinement_mode=TEST_PROCESS_CONFINEMENT, allowed_executables=["echo"], default_timeout_seconds=5.0)
    adapter = ShellCommandAdapter(executor, sandbox=fs_for_process)
    out = await adapter.execute(invocation(
        "system.shell", "run_shell_command", arguments={"argv": ["echo", "hi"]},
    ))
    assert out.ok
    assert "hi" in out.content


async def test_shell_adapter_refuses_unauthorized_executable(fs_for_process):
    executor = ConstrainedProcessExecutor(confinement_mode=TEST_PROCESS_CONFINEMENT, allowed_executables=["echo"])
    adapter = ShellCommandAdapter(executor, sandbox=fs_for_process)
    out = await adapter.execute(invocation(
        "system.shell", "run_shell_command", arguments={"argv": ["rm", "-rf", "/"]},
    ))
    assert not out.ok
    assert out.error == "unauthorized_executable"


async def test_shell_adapter_runs_in_the_tasks_own_sandbox_temp(fs_for_process):
    executor = ConstrainedProcessExecutor(confinement_mode=TEST_PROCESS_CONFINEMENT, allowed_executables=["pwd"])
    adapter = ShellCommandAdapter(executor, sandbox=fs_for_process)
    task_id = uuid.uuid4()
    out = await adapter.execute(invocation(
        "system.shell", "run_shell_command", task_id=task_id, arguments={"argv": ["pwd"]},
    ))
    assert out.ok
    expected = fs_for_process.task_temp(task_id=task_id)
    assert out.content.strip() == expected


async def test_shell_adapter_rejects_non_list_argv(fs_for_process):
    executor = ConstrainedProcessExecutor(confinement_mode=TEST_PROCESS_CONFINEMENT, allowed_executables=["echo"])
    adapter = ShellCommandAdapter(executor, sandbox=fs_for_process)
    out = await adapter.execute(invocation(
        "system.shell", "run_shell_command", arguments={"argv": "echo hi"},
    ))
    assert not out.ok
    assert out.error == "invalid_arguments"


def test_shell_command_tool_registers_and_is_isolated_from_ordinary_capabilities(fs_for_process):
    from server.capabilities.floor import floor_category_for_capability

    registry = ToolRegistry()
    executor = ConstrainedProcessExecutor()
    registry.register(shell_command_tool(executor, sandbox=fs_for_process), enabled=True)
    handle = registry.resolve("system.shell")
    assert handle is not None
    assert handle.required_capability == "system.restricted"
    assert floor_category_for_capability(handle.required_capability) is None  # governed, not floor


# ── android adapter ───────────────────────────────────────────────────────


class _FakeDeviceTransport:
    def __init__(self) -> None:
        self.sent: list[DeviceOperation] = []

    async def send(self, operation: DeviceOperation):
        self.sent.append(operation)
        from shared.schemas.execution import ExecutionResult

        return ExecutionResult(content="tapped")

    async def is_connected(self, *, user_id):
        return True


async def test_android_adapter_dispatches_through_the_transport():
    transport = _FakeDeviceTransport()
    adapter = AndroidDeviceAdapter("app.interact", transport)
    out = await adapter.execute(invocation(
        "device.app_interact", "tap", arguments={"x": 1, "y": 2},
        scope={"package_name": "com.example"},
    ))
    assert out.ok
    assert out.content == "tapped"
    assert transport.sent[0].package_name == "com.example"
    assert transport.sent[0].device_id is not None


async def test_android_adapter_defaults_to_unavailable_and_fails_closed():
    adapter = AndroidDeviceAdapter("app.interact", UnavailableDeviceTransport())
    out = await adapter.execute(invocation(
        "device.app_interact", "tap", arguments={}, scope={"package_name": "com.example"},
    ))
    assert not out.ok
    assert out.error == "device_unavailable"


def test_android_tool_registers_with_the_default_unavailable_transport():
    registry = ToolRegistry()
    registry.register(android_app_interact_tool(), enabled=True)
    handle = registry.resolve("device.app_interact")
    assert handle is not None
    assert ExecutionPlatform.ANDROID in handle.platforms
