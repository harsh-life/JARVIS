"""server/execution/android.py — 08_ANDROID_SHIZUKU.md's server-side half.

08 §4's device-side re-check lives in the Android client (`android/`) and is
held to the same table through `shared/android/device_mapping.json`; what these tests hold this
module to is the server-side half it owns: every enumerated capability/operation
pair maps to a named primitive and nothing else is dispatchable (AND-006),
a malformed/unmapped op is rejected before ever reaching a transport
(08 §7), and the shipped default transport fails every operation
deterministically rather than hanging or silently no-op'ing.
"""

from __future__ import annotations

import uuid

import pytest

from server.execution.android import (
    PRIMITIVE_BY_OPERATION,
    DeviceOperation,
    UnavailableDeviceTransport,
    build_operation,
)
from shared.schemas.execution import ExecutionError, ExecutionErrorCode, ExecutionResult


def _ids() -> tuple[uuid.UUID, uuid.UUID]:
    return uuid.uuid4(), uuid.uuid4()


# ── AND-T1: enumerated mapping, nothing outside it dispatchable ─────────


def test_every_capability_operation_pair_has_a_named_primitive():
    for capability, operations in PRIMITIVE_BY_OPERATION.items():
        for operation, primitive in operations.items():
            assert isinstance(primitive, str) and primitive


def test_app_interact_matches_the_capability_registrys_enumerated_operations():
    """The mapping here must not silently drift from what `04`/`07` actually
    authorize (server/capabilities/registry.py) — a primitive existing for
    an operation the registry doesn't enumerate would be dead code; the
    reverse would make an authorized operation undispatchable."""

    from server.capabilities.registry import lookup

    registry_ops = set(lookup("app.interact").operations)
    mapped_ops = set(PRIMITIVE_BY_OPERATION["app.interact"])
    assert registry_ops == mapped_ops


def test_device_read_and_ui_control_match_the_registry_too():
    from server.capabilities.registry import lookup

    for capability in ("device.read", "device.ui_control"):
        assert set(lookup(capability).operations) == set(PRIMITIVE_BY_OPERATION[capability])


def test_system_restricted_has_no_android_mapping_at_all():
    """08 §6 / docs/23 §5.3 (AND-T7): the elevated Shizuku shell stays
    unexposed in this build — `system.restricted` is not dispatchable to a
    phone, whatever the server authorizes."""

    assert "system.restricted" not in PRIMITIVE_BY_OPERATION
    user_id, task_id = _ids()
    with pytest.raises(ExecutionError) as excinfo:
        build_operation(
            capability="system.restricted", operation="run_shell_command",
            package_name=None, arguments={"argv": ["id"]}, user_id=user_id, task_id=task_id,
            device_id=uuid.uuid4(),
        )
    assert excinfo.value.code == ExecutionErrorCode.PLATFORM_UNSUPPORTED


def test_unmapped_capability_is_rejected_before_reaching_a_transport():
    user_id, task_id = _ids()
    with pytest.raises(ExecutionError) as excinfo:
        build_operation(
            capability="not.a.real.capability", operation="anything",
            package_name=None, arguments={}, user_id=user_id, task_id=task_id, device_id=uuid.uuid4(),
        )
    assert excinfo.value.code == ExecutionErrorCode.PLATFORM_UNSUPPORTED


def test_unmapped_operation_within_a_known_capability_is_rejected():
    """AND-006: 'app.interact on WhatsApp means exactly the enumerated UI
    operations... not "do anything to WhatsApp."' An operation name the
    capability does not enumerate must fail even though the capability
    itself is real."""

    user_id, task_id = _ids()
    with pytest.raises(ExecutionError) as excinfo:
        build_operation(
            capability="app.interact", operation="delete_app",
            package_name="com.example", arguments={}, user_id=user_id, task_id=task_id, device_id=uuid.uuid4(),
        )
    assert excinfo.value.code == ExecutionErrorCode.PLATFORM_UNSUPPORTED


def test_valid_operation_builds_a_well_formed_device_operation():
    user_id, task_id = _ids()
    op = build_operation(
        capability="app.interact", operation="tap",
        package_name="com.example.app", arguments={"view_id": "send_button"},
        user_id=user_id, task_id=task_id, device_id=uuid.uuid4(),
    )
    assert isinstance(op, DeviceOperation)
    assert op.primitive == "accessibility.tap"
    assert op.package_name == "com.example.app"
    assert op.arguments == {"view_id": "send_button"}


def test_system_restricted_is_kept_out_of_ordinary_capability_mappings():
    """08 §6: shell-like execution stays a separate, isolated family — no
    ordinary capability's mapping may alias into it."""

    for capability in ("app.interact", "device.read", "device.ui_control"):
        assert "run_shell_command" not in PRIMITIVE_BY_OPERATION[capability].values()
        assert "shizuku.elevated_shell" not in PRIMITIVE_BY_OPERATION[capability].values()


# ── AND-T5/T6/T7: fail-closed when no device is available ───────────────


async def test_unavailable_transport_fails_every_operation_deterministically():
    user_id, task_id = _ids()
    op = build_operation(
        capability="device.read", operation="read_battery",
        package_name=None, arguments={}, user_id=user_id, task_id=task_id, device_id=uuid.uuid4(),
    )
    transport = UnavailableDeviceTransport()
    with pytest.raises(ExecutionError) as excinfo:
        await transport.send(op)
    assert excinfo.value.code == ExecutionErrorCode.DEVICE_UNAVAILABLE


async def test_unavailable_transport_reports_not_connected():
    transport = UnavailableDeviceTransport()
    assert await transport.is_connected(device_id=uuid.uuid4()) is False


async def test_unavailable_transport_never_returns_a_fabricated_success():
    """FAIL-CORE-001/002: a dependency outage never produces a fabricated
    answer. There is no code path in UnavailableDeviceTransport.send that
    returns an ExecutionResult at all."""

    import inspect

    from server.execution import android as android_module

    source = inspect.getsource(android_module.UnavailableDeviceTransport.send)
    assert "ExecutionResult(" not in source


# ── a working fake transport proves the dispatch shape end to end ───────


class _RecordingTransport:
    def __init__(self) -> None:
        self.sent: list[DeviceOperation] = []

    async def send(self, operation: DeviceOperation) -> ExecutionResult:
        self.sent.append(operation)
        return ExecutionResult(content="ok", metadata={"primitive": operation.primitive})

    async def is_connected(self, *, device_id: uuid.UUID) -> bool:
        return True


async def test_a_connected_transport_receives_exactly_the_built_operation():
    user_id, task_id = _ids()
    op = build_operation(
        capability="app.interact", operation="read_screen_element",
        package_name="com.example", arguments={"view_id": "login"},
        user_id=user_id, task_id=task_id, device_id=uuid.uuid4(),
    )
    transport = _RecordingTransport()
    result = await transport.send(op)
    assert result.metadata["primitive"] == "accessibility.read_element"
    assert transport.sent == [op]


# ── integration hardening: dispatch is bound to one device and one app ──


def test_a_device_operation_without_the_authorizing_device_is_refused():
    """A grant can be device-scoped (PRD §13's grid is per phone). An operation
    that names no device would let a transport run it on any of the user's
    devices, including one where nothing was granted."""

    user_id, task_id = _ids()
    with pytest.raises(ExecutionError) as excinfo:
        build_operation(
            capability="device.read", operation="read_battery",
            package_name=None, arguments={}, user_id=user_id, task_id=task_id, device_id=None,
        )
    assert excinfo.value.code == ExecutionErrorCode.MISSING_CONTEXT


def test_app_interact_without_a_named_app_is_refused():
    """08 §2: app.interact acts inside one named app — never "whatever app is
    in front"."""

    user_id, task_id = _ids()
    with pytest.raises(ExecutionError) as excinfo:
        build_operation(
            capability="app.interact", operation="tap", package_name=None,
            arguments={"view_id": "send"}, user_id=user_id, task_id=task_id, device_id=uuid.uuid4(),
        )
    assert excinfo.value.code == ExecutionErrorCode.MISSING_CONTEXT


def test_the_built_operation_carries_exactly_the_authorizing_device():
    user_id, task_id = _ids()
    device_id = uuid.uuid4()
    op = build_operation(
        capability="app.interact", operation="tap", package_name="com.example",
        arguments={"text": "Send"}, user_id=user_id, task_id=task_id, device_id=device_id,
    )
    assert op.device_id == device_id
