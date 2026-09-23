"""server/execution/android.py — 08_ANDROID_SHIZUKU.md's server-side half.

08 §4 needs a device-side re-check this repository does not contain
(`android/` does not exist yet); what these tests hold this module to is the
server-side half it actually owns: every enumerated capability/operation
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

    for capability in ("device.read", "device.ui_control", "system.restricted"):
        assert set(lookup(capability).operations) == set(PRIMITIVE_BY_OPERATION[capability])


def test_unmapped_capability_is_rejected_before_reaching_a_transport():
    user_id, task_id = _ids()
    with pytest.raises(ExecutionError) as excinfo:
        build_operation(
            capability="not.a.real.capability", operation="anything",
            package_name=None, arguments={}, user_id=user_id, task_id=task_id,
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
            package_name="com.example", arguments={}, user_id=user_id, task_id=task_id,
        )
    assert excinfo.value.code == ExecutionErrorCode.PLATFORM_UNSUPPORTED


def test_valid_operation_builds_a_well_formed_device_operation():
    user_id, task_id = _ids()
    op = build_operation(
        capability="app.interact", operation="tap",
        package_name="com.example.app", arguments={"x": 10, "y": 20},
        user_id=user_id, task_id=task_id,
    )
    assert isinstance(op, DeviceOperation)
    assert op.primitive == "accessibility.tap"
    assert op.package_name == "com.example.app"
    assert op.arguments == {"x": 10, "y": 20}


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
        package_name=None, arguments={}, user_id=user_id, task_id=task_id,
    )
    transport = UnavailableDeviceTransport()
    with pytest.raises(ExecutionError) as excinfo:
        await transport.send(op)
    assert excinfo.value.code == ExecutionErrorCode.DEVICE_UNAVAILABLE


async def test_unavailable_transport_reports_not_connected():
    transport = UnavailableDeviceTransport()
    assert await transport.is_connected(user_id=uuid.uuid4()) is False


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

    async def is_connected(self, *, user_id: uuid.UUID) -> bool:
        return True


async def test_a_connected_transport_receives_exactly_the_built_operation():
    user_id, task_id = _ids()
    op = build_operation(
        capability="app.interact", operation="read_screen_element",
        package_name="com.example", arguments={"selector": "id/login"},
        user_id=user_id, task_id=task_id,
    )
    transport = _RecordingTransport()
    result = await transport.send(op)
    assert result.metadata["primitive"] == "accessibility.read_element"
    assert transport.sent == [op]
