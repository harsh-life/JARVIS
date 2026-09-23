"""The Android/Shizuku execution boundary (08_ANDROID_SHIZUKU.md) — the
server-side half.

08 §4 (AND-003) requires **two independent layers** of enforcement: the
server (`04`/`07`, already run before anything here is reached) and the
device itself, which "does not trust the server blindly and does not
execute an op the user has toggled off, even if the server sent it." The
device-side half is Android application code (`android/`, 16 §1) that does
not exist in this repository yet — this module is honest about owning only
the server side of that pair, never claiming to close a loop it cannot see
the other half of.

What this module *does* provide, fully: the typed `DeviceOperation` a
platform adapter builds from an already-authorized `ExecutionRequest`, the
capability→operation→primitive mapping table 08 §2/AND-006 requires to be
enumerable (never "do anything to this app"), the `DeviceTransport`
Protocol a real device channel implements, and — because no real channel
exists yet — `UnavailableDeviceTransport`, which fails every operation
deterministically (`ExecutionErrorCode.DEVICE_UNAVAILABLE`) rather than the
call hanging, silently no-op'ing, or (08 §7) being "best-effort" executed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol
from uuid import UUID

from shared.schemas.execution import ExecutionError, ExecutionErrorCode, ExecutionResult

# 08 §2 / AND-006, illustrative table transcribed from docs/CAPABILITY_MATRIX.md
# §3.1 and server/capabilities/registry.py's enumerated operation sets. This is
# presentational/dispatch metadata only — it grants nothing; the operation is
# only reachable at all once `04`/`07` have already authorized it against the
# *same* enumerated set in the capability registry. A capability with no entry
# here cannot be dispatched even if somehow authorized, which cannot happen
# (the registry is the single source of the enumeration both sides read).
PRIMITIVE_BY_OPERATION: Mapping[str, Mapping[str, str]] = {
    "app.interact": {
        "tap": "accessibility.tap",
        "swipe": "accessibility.swipe",
        "input_text": "accessibility.input_text",
        "read_screen_element": "accessibility.read_element",
        "launch_activity": "android.intent.launch_activity",
    },
    "device.read": {
        "read_screen": "accessibility.read_tree",
        "read_battery": "android.api.battery_state",
        "read_notification": "android.api.notification_query",
    },
    "device.ui_control": {
        "tap": "accessibility.tap",
        "swipe": "accessibility.swipe",
        "input_text": "accessibility.input_text",
        "global_action": "accessibility.global_action",
    },
    # 08 §6: kept deliberately separate, high-risk, never folded into an
    # ordinary capability's mapping.
    "system.restricted": {
        "run_shell_command": "shizuku.elevated_shell",
    },
}


@dataclass(frozen=True)
class DeviceOperation:
    """What a platform adapter sends to a `DeviceTransport` — built only from
    an already-authorized `ExecutionRequest` (`shared.schemas.execution`),
    never from raw agent/model input. Carries no authority of its own: a
    transport that receives one is trusted to have been called only after
    `04`/`07` already authorized it, exactly as every other primitive in
    this package assumes (see `shared/schemas/execution.py`'s module
    docstring)."""

    capability: str
    operation: str
    primitive: str
    package_name: str | None
    arguments: Mapping[str, object]
    user_id: UUID
    task_id: UUID


class DeviceUnavailable(Exception):
    """No transport is connected for this principal's device right now."""


class DeviceTransport(Protocol):
    """What a real device channel (a websocket/push connection to the phone,
    08 §1's Accessibility/Shizuku/API mechanisms on the other end)
    implements. `server/tools/platforms.py`'s Android adapter is written
    against this Protocol, never against a concrete transport, so swapping
    in a real one (when `android/` exists) is a configuration change, not a
    rewrite of the authorization or dispatch path (P4)."""

    async def send(self, operation: DeviceOperation) -> ExecutionResult: ...

    async def is_connected(self, *, user_id: UUID) -> bool: ...


def build_operation(
    *,
    capability: str,
    operation: str,
    package_name: str | None,
    arguments: Mapping[str, object],
    user_id: UUID,
    task_id: UUID,
) -> DeviceOperation:
    """The one constructor a platform adapter uses. Fails closed
    (`ExecutionErrorCode.PLATFORM_UNSUPPORTED`) for a capability/operation
    pair with no enumerated primitive — 08 §7: a malformed or out-of-mapping
    op is rejected here, before it ever reaches a transport, never
    "best-effort" forwarded."""

    by_operation = PRIMITIVE_BY_OPERATION.get(capability)
    if by_operation is None:
        raise ExecutionError(
            ExecutionErrorCode.PLATFORM_UNSUPPORTED,
            f"capability {capability!r} has no Android primitive mapping",
        )
    primitive = by_operation.get(operation)
    if primitive is None:
        raise ExecutionError(
            ExecutionErrorCode.PLATFORM_UNSUPPORTED,
            f"operation {operation!r} is not in {capability!r}'s enumerated Android mapping (AND-006)",
        )
    return DeviceOperation(
        capability=capability, operation=operation, primitive=primitive,
        package_name=package_name, arguments=dict(arguments), user_id=user_id, task_id=task_id,
    )


class UnavailableDeviceTransport:
    """The only transport this branch ships. No Android client connection
    exists in this repository yet (`android/` is not built — 16 §1), so
    every operation fails deterministically rather than hanging, silently
    no-op'ing, or (worse) being reported as having succeeded. Registering an
    Android tool adapter with this transport as the default is a deliberate
    choice: the capability surface exists and is fully specified (this
    module's mapping table, 07/08's authorization chain), but nothing
    executes until a real transport is configured — matching PRD §3
    ("disabled, not erroring", `P3`) applied to a still-unbuilt adapter
    rather than an unbuilt provider.
    """

    async def send(self, operation: DeviceOperation) -> ExecutionResult:
        raise ExecutionError(
            ExecutionErrorCode.DEVICE_UNAVAILABLE,
            f"no device transport is connected for this task (op={operation.primitive})",
        )

    async def is_connected(self, *, user_id: UUID) -> bool:
        return False


__all__ = [
    "PRIMITIVE_BY_OPERATION",
    "DeviceOperation",
    "DeviceTransport",
    "DeviceUnavailable",
    "UnavailableDeviceTransport",
    "build_operation",
]
