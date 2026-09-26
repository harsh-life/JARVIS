"""The Android execution boundary (08_ANDROID_SHIZUKU.md, docs/23) — the
server-side half.

08 §4 (AND-003) requires **two independent layers** of enforcement: the
server (`04`/`07`, already run before anything here is reached) and the
device, which "does not trust the server blindly and does not execute an op
the user has toggled off, even if the server sent it". The device half is the
Android client (`android/`); both halves read **one** table — this module's
`DEVICE_MAPPING`, exported as `shared/android/device_mapping.json` and bundled
into the client build (docs/23 §5.1). The device executes a triple only if it
is in the table *of the same version* the server used, so the two layers
cannot drift apart silently: a changed table changes `MAPPING_VERSION`, and a
device on the old table refuses (ANDC-T4).

The table is presentational/dispatch metadata. It grants nothing: an
operation reaches it only after the capability registry (the single source of
the enumerated operation set and its tiers) and the engine have authorized
it. What the table adds is *how* each operation runs on a phone — the
primitive, its mechanism, which per-app grid toggle governs it, whether it
acts inside one named app, the on-device dependencies it needs, the exact
argument shape, and how large a result may be.

`system.restricted` has **no** Android entry. 08 §6/docs/23 §5.3: the
elevated Shizuku shell stays unexposed; the only Shizuku primitives are typed
ones listed here, each with a fixed argument shape (no argv, no command
string, nothing a model can use to pick a raw command).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping, Protocol
from uuid import UUID

from shared.schemas.device_channel import (
    PACKAGE_NAME_PATTERN,
    DEFAULT_OPERATION_TTL,
    MAX_OPERATION_TTL,
    MAX_RESULT_FRAME_BYTES,
    MAX_SCREENSHOT_FRAME_BYTES,
    DeviceMechanism,
    DeviceOperationEnvelope,
    DevicePlatformDependency,
    GridToggle,
    ResultKind,
)
from shared.schemas.execution import ExecutionError, ExecutionErrorCode, ExecutionResult

_PACKAGE_NAME = re.compile(PACKAGE_NAME_PATTERN)

# ── the mapping table (08 §2 / AND-006, docs/23 §5.1) ─────────────────────

# Bumped by hand only when the *shape* of the exported document changes. Any
# change to the table's content changes `MAPPING_VERSION` through its digest.
MAPPING_SCHEMA = 1

MAPPING_ARTIFACT_PATH = (
    Path(__file__).resolve().parents[2] / "shared" / "android" / "device_mapping.json"
)

ArgumentKind = Literal["string", "int", "bool", "enum"]
PackageScope = Literal["required", "forbidden"]


@dataclass(frozen=True)
class ArgumentSpec:
    """One argument's exact shape. The Android client implements the same
    rules (`android/contract/.../ArgumentValidator.kt`), and the conformance
    vectors hold both to identical verdicts."""

    kind: ArgumentKind
    required: bool = False
    max_length: int | None = None
    minimum: int | None = None
    maximum: int | None = None
    values: tuple[str, ...] = ()

    def document(self) -> dict[str, Any]:
        doc: dict[str, Any] = {"kind": self.kind, "required": self.required}
        if self.max_length is not None:
            doc["max_length"] = self.max_length
        if self.minimum is not None:
            doc["minimum"] = self.minimum
        if self.maximum is not None:
            doc["maximum"] = self.maximum
        if self.values:
            doc["values"] = list(self.values)
        return doc


@dataclass(frozen=True)
class PrimitiveSpec:
    primitive: str
    mechanism: DeviceMechanism
    grid_toggle: GridToggle
    # `required`: the operation acts inside exactly one named app, and the
    # device checks that app is the one in front before acting (docs/23 §5.2 —
    # it never substitutes another package). `forbidden`: a device-level read
    # that names no app.
    package_scope: PackageScope
    dependencies: tuple[DevicePlatformDependency, ...]
    arguments: Mapping[str, ArgumentSpec] = field(default_factory=dict)
    # Exactly one of these argument groups must be present (a UI target is
    # named one way, never ambiguously).
    one_of: tuple[tuple[str, ...], ...] = ()
    max_result_bytes: int = MAX_RESULT_FRAME_BYTES
    # The shape an `ok` result must have. The server validates every device
    # result against it before anything reaches the worker (untrusted input,
    # PRD §24); the device builds exactly that shape.
    result: ResultKind = ResultKind.ACTION

    def document(self) -> dict[str, Any]:
        return {
            "primitive": self.primitive,
            "mechanism": self.mechanism.value,
            "grid_toggle": self.grid_toggle.value,
            "package_scope": self.package_scope,
            "dependencies": [d.value for d in self.dependencies],
            "arguments": {name: spec.document() for name, spec in sorted(self.arguments.items())},
            "one_of": [list(group) for group in self.one_of],
            "max_result_bytes": self.max_result_bytes,
            "result": self.result.value,
        }


_A11Y = (DevicePlatformDependency.ACCESSIBILITY_SERVICE,)

# A UI target is named by what the Accessibility tree exposes — never by raw
# screen coordinates. 08 §7: an op whose target is absent "fails as an
# observation ... it does not blindly tap coordinates".
_SELECTOR_ARGS: Mapping[str, ArgumentSpec] = {
    "view_id": ArgumentSpec("string", max_length=200),
    "text": ArgumentSpec("string", max_length=200),
    "content_description": ArgumentSpec("string", max_length=200),
    "index": ArgumentSpec("int", minimum=0, maximum=50),
}
_SELECTOR_ONE_OF = (("view_id",), ("text",), ("content_description",))

_TAP = PrimitiveSpec(
    "accessibility.tap", DeviceMechanism.ACCESSIBILITY, GridToggle.UI_INTERACTION,
    "required", _A11Y, _SELECTOR_ARGS, _SELECTOR_ONE_OF, 4096,
)
_SWIPE = PrimitiveSpec(
    "accessibility.swipe", DeviceMechanism.ACCESSIBILITY, GridToggle.UI_INTERACTION,
    "required", _A11Y,
    {
        "direction": ArgumentSpec("enum", required=True, values=("up", "down", "left", "right")),
        "view_id": ArgumentSpec("string", max_length=200),
    },
    (), 4096,
)
_INPUT_TEXT = PrimitiveSpec(
    "accessibility.input_text", DeviceMechanism.ACCESSIBILITY, GridToggle.UI_INTERACTION,
    "required", _A11Y,
    {
        "text": ArgumentSpec("string", required=True, max_length=2000),
        "view_id": ArgumentSpec("string", max_length=200),
    },
    (), 4096,
)

DEVICE_MAPPING: Mapping[str, Mapping[str, PrimitiveSpec]] = MappingProxyType(
    {
        "app.interact": MappingProxyType(
            {
                "tap": _TAP,
                "swipe": _SWIPE,
                "input_text": _INPUT_TEXT,
                "read_screen_element": PrimitiveSpec(
                    "accessibility.read_element", DeviceMechanism.ACCESSIBILITY,
                    GridToggle.SCREEN_READ, "required", _A11Y, _SELECTOR_ARGS, _SELECTOR_ONE_OF,
                    result=ResultKind.SCREEN_READ,
                ),
                "launch_activity": PrimitiveSpec(
                    "android.intent.launch_activity", DeviceMechanism.ANDROID_API,
                    GridToggle.UI_INTERACTION, "required", (), {}, (), 4096,
                ),
                # [PROPOSED] (docs/CAPABILITY_MATRIX.md §3.1): the one primitive in
                # this build that genuinely needs Shizuku — force-stopping another
                # app requires shell-level privilege no Accessibility action or
                # public API grants. A typed call with no arguments; the package
                # is the envelope's own `package_name`, validated on both sides.
                "force_stop": PrimitiveSpec(
                    "shizuku.force_stop_package", DeviceMechanism.SHIZUKU,
                    GridToggle.UI_INTERACTION, "required",
                    (DevicePlatformDependency.SHIZUKU,), {}, (), 4096,
                ),
            }
        ),
        "device.read": MappingProxyType(
            {
                # The perception ladder (docs/23 §6): Accessibility → app
                # metadata → on-device OCR. Never a screenshot — that is its
                # own operation below.
                "read_screen": PrimitiveSpec(
                    "accessibility.read_tree", DeviceMechanism.ACCESSIBILITY,
                    GridToggle.SCREEN_READ, "required", _A11Y, result=ResultKind.SCREEN_READ,
                ),
                "read_battery": PrimitiveSpec(
                    "android.api.battery_state", DeviceMechanism.ANDROID_API,
                    GridToggle.DEVICE_STATE, "forbidden", (), {}, (), 4096, ResultKind.BATTERY,
                ),
                "read_notification": PrimitiveSpec(
                    "android.api.notification_query", DeviceMechanism.ANDROID_API,
                    GridToggle.SCREEN_READ, "required",
                    (DevicePlatformDependency.NOTIFICATION_ACCESS,),
                    {"limit": ArgumentSpec("int", minimum=1, maximum=50)},
                    result=ResultKind.NOTIFICATIONS,
                ),
                # [PROPOSED] OD-AND-4 (docs/23 §6 level 4). Its own grid toggle,
                # off by default on the device, and refused for FLAG_SECURE
                # windows and sensitive packages.
                "capture_screenshot": PrimitiveSpec(
                    "accessibility.screenshot", DeviceMechanism.ACCESSIBILITY,
                    GridToggle.SCREENSHOT, "required",
                    (DevicePlatformDependency.ACCESSIBILITY_SERVICE, DevicePlatformDependency.SCREEN_CAPTURE),
                    {}, (), MAX_SCREENSHOT_FRAME_BYTES, ResultKind.SCREENSHOT,
                ),
            }
        ),
        "device.ui_control": MappingProxyType(
            {
                "tap": _TAP,
                "swipe": _SWIPE,
                "input_text": _INPUT_TEXT,
                "global_action": PrimitiveSpec(
                    "accessibility.global_action", DeviceMechanism.ACCESSIBILITY,
                    GridToggle.UI_INTERACTION, "required", _A11Y,
                    {
                        "action": ArgumentSpec(
                            "enum", required=True, values=("back", "home", "recents", "notifications")
                        )
                    },
                    (), 4096,
                ),
            }
        ),
    }
)

# The plain capability → operation → primitive view (08 §2's table).
PRIMITIVE_BY_OPERATION: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        capability: MappingProxyType({op: spec.primitive for op, spec in ops.items()})
        for capability, ops in DEVICE_MAPPING.items()
    }
)


def mapping_document() -> dict[str, Any]:
    """The table as data, without its version (the version is its digest)."""

    return {
        "schema": MAPPING_SCHEMA,
        "capabilities": {
            capability: {op: spec.document() for op, spec in sorted(ops.items())}
            for capability, ops in sorted(DEVICE_MAPPING.items())
        },
    }


def canonical_json(document: Mapping[str, Any]) -> str:
    """Sorted keys, no whitespace, ASCII only. The Android contract module
    reproduces exactly this serialization to recompute the digest, so the
    bundled file cannot be hand-edited without the client noticing."""

    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _compute_version(document: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json(document).encode("ascii")).hexdigest()
    return f"{MAPPING_SCHEMA}-{digest[:16]}"


MAPPING_VERSION = _compute_version(mapping_document())


def export_mapping() -> str:
    """The shared artifact's exact bytes (docs/23 §5.1)."""

    document = mapping_document()
    document["mapping_version"] = MAPPING_VERSION
    return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def check_artifact() -> bool:
    """Whether the committed shared artifact equals the table (drift check)."""

    return (
        MAPPING_ARTIFACT_PATH.exists()
        and MAPPING_ARTIFACT_PATH.read_text(encoding="ascii") == export_mapping()
    )


def primitive_spec(capability: str, operation: str) -> PrimitiveSpec:
    by_operation = DEVICE_MAPPING.get(capability)
    if by_operation is None:
        raise ExecutionError(
            ExecutionErrorCode.PLATFORM_UNSUPPORTED,
            f"capability {capability!r} has no Android primitive mapping",
        )
    spec = by_operation.get(operation)
    if spec is None:
        raise ExecutionError(
            ExecutionErrorCode.PLATFORM_UNSUPPORTED,
            f"operation {operation!r} is not in {capability!r}'s enumerated Android mapping (AND-006)",
        )
    return spec


def argument_problem(spec: PrimitiveSpec, arguments: Mapping[str, Any]) -> str | None:
    """Why `arguments` is not well formed for `spec`, or `None`.

    08 §7 (AND-T6): a malformed operation is rejected, never "best-effort"
    executed — so an unknown key is an error, not ignored. The device runs the
    same check independently (docs/23 §5.2 step 4)."""

    if not isinstance(arguments, Mapping):
        return "arguments must be an object"
    unknown = set(arguments) - set(spec.arguments)
    if unknown:
        return f"unknown argument(s) {sorted(unknown)}"
    for name, arg in spec.arguments.items():
        if name not in arguments:
            if arg.required:
                return f"missing required argument {name!r}"
            continue
        value = arguments[name]
        if arg.kind in ("string", "enum"):
            if not isinstance(value, str) or not value:
                return f"{name!r} must be a non-empty string"
            if arg.max_length is not None and len(value) > arg.max_length:
                return f"{name!r} exceeds {arg.max_length} characters"
            if arg.kind == "enum" and value not in arg.values:
                return f"{name!r} must be one of {list(arg.values)}"
        elif arg.kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                return f"{name!r} must be an integer"
            if arg.minimum is not None and value < arg.minimum:
                return f"{name!r} is below {arg.minimum}"
            if arg.maximum is not None and value > arg.maximum:
                return f"{name!r} is above {arg.maximum}"
        elif arg.kind == "bool":
            if not isinstance(value, bool):
                return f"{name!r} must be a boolean"
    if spec.one_of:
        grouped = {name for group in spec.one_of for name in group}
        present = grouped & set(arguments)
        matches = [group for group in spec.one_of if set(group) == present]
        if len(matches) != 1:
            return f"exactly one of {[list(g) for g in spec.one_of]} must be given"
    return None


# ── the operation a transport delivers ───────────────────────────────────


@dataclass(frozen=True)
class DeviceOperation:
    """What a platform adapter hands a `DeviceTransport` — built only by
    `build_operation` from an already-authorized `ExecutionRequest`, never
    from raw agent/model input. Carries no authority of its own.

    `user_id` stays server-side (the envelope the device receives omits it:
    the device is bound to exactly one user by its credential already)."""

    op_id: UUID
    capability: str
    operation: str
    primitive: str
    package_name: str | None
    arguments: Mapping[str, object]
    user_id: UUID
    task_id: UUID
    # The one device this operation may run on: the authorizing principal's
    # own (03 §8, OD-DEV-1). Never "any connected device of this user", which
    # may be one where the user granted nothing (PRD §13's grid is per device).
    device_id: UUID
    mapping_version: str
    issued_at: datetime
    expires_at: datetime
    max_result_bytes: int

    def envelope(self) -> DeviceOperationEnvelope:
        return DeviceOperationEnvelope(
            op_id=self.op_id, task_id=self.task_id, device_id=self.device_id,
            capability=self.capability, operation=self.operation, primitive=self.primitive,
            package_name=self.package_name, arguments=dict(self.arguments),
            mapping_version=self.mapping_version, issued_at=self.issued_at,
            expires_at=self.expires_at,
        )


def build_operation(
    *,
    capability: str,
    operation: str,
    package_name: str | None,
    arguments: Mapping[str, object],
    user_id: UUID,
    task_id: UUID,
    device_id: UUID | None,
    now: datetime | None = None,
    ttl: timedelta = DEFAULT_OPERATION_TTL,
) -> DeviceOperation:
    """The one constructor a platform adapter uses. Fails closed before
    anything reaches a transport (08 §7): an unmapped pair is
    `PLATFORM_UNSUPPORTED`; a missing device or app is `MISSING_CONTEXT`;
    malformed arguments are `INVALID_ARGUMENTS`. Every call mints a fresh
    `op_id` and a fresh, short validity window — an operation is never
    re-sent under an old identity."""

    spec = primitive_spec(capability, operation)
    if device_id is None:
        raise ExecutionError(
            ExecutionErrorCode.MISSING_CONTEXT,
            "a device operation must name the authorizing principal's device",
        )
    if spec.package_scope == "required":
        if not package_name:
            raise ExecutionError(
                ExecutionErrorCode.MISSING_CONTEXT,
                f"{capability}.{operation} acts inside one named app — resource_scope must carry package_name",
            )
        if not _PACKAGE_NAME.match(package_name):
            # Refused here, as a typed error, rather than surfacing as a raw
            # validation failure when the envelope is built.
            raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, "package_name is not an Android package name")
    else:
        # A device-level read names no app; a package here would be a
        # narrowing the device could not honour, so it is simply not sent.
        package_name = None
    problem = argument_problem(spec, arguments)
    if problem is not None:
        raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, problem)
    if ttl > MAX_OPERATION_TTL:
        raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, "operation TTL exceeds the maximum")
    issued_at = now or datetime.now(timezone.utc)
    return DeviceOperation(
        op_id=uuid.uuid4(), capability=capability, operation=operation, primitive=spec.primitive,
        package_name=package_name, arguments=dict(arguments), user_id=user_id, task_id=task_id,
        device_id=device_id, mapping_version=MAPPING_VERSION, issued_at=issued_at,
        expires_at=issued_at + ttl, max_result_bytes=spec.max_result_bytes,
    )


# ── the transport Protocol ───────────────────────────────────────────────


class DeviceUnavailable(Exception):
    """No transport is connected for this device right now."""


class DeviceTransport(Protocol):
    """What a device channel implements (docs/23 §4). `server/tools/
    platforms.py`'s Android adapter is written against this Protocol, never a
    concrete transport."""

    async def send(self, operation: DeviceOperation) -> ExecutionResult: ...

    # docs/23 §4 code delta: device-scoped. The old user-scoped signature could
    # report a user's *other* device as connected (ANDC-T1).
    async def is_connected(self, *, device_id: UUID) -> bool: ...


class UnavailableDeviceTransport:
    """The transport used when the device channel is disabled
    (`android.enabled: false`). Every operation fails deterministically
    rather than hanging, silently no-op'ing, or being reported as done."""

    async def send(self, operation: DeviceOperation) -> ExecutionResult:
        raise ExecutionError(
            ExecutionErrorCode.DEVICE_UNAVAILABLE,
            f"no device transport is connected for this task (op={operation.primitive})",
        )

    async def is_connected(self, *, device_id: UUID) -> bool:
        return False


__all__ = [
    "DEVICE_MAPPING",
    "MAPPING_ARTIFACT_PATH",
    "MAPPING_VERSION",
    "PRIMITIVE_BY_OPERATION",
    "ArgumentSpec",
    "DeviceOperation",
    "DeviceTransport",
    "DeviceUnavailable",
    "PrimitiveSpec",
    "UnavailableDeviceTransport",
    "argument_problem",
    "build_operation",
    "canonical_json",
    "check_artifact",
    "export_mapping",
    "mapping_document",
    "primitive_spec",
]
