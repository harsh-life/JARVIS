"""The server-side fake device (docs/23 §8) and the reference device guard.

`reference_guard` is the device-side guard of docs/23 §5.2 written in Python —
the executable statement of the rules the Android client's `DeviceGuard`
(`android/contract/.../DeviceGuard.kt`) implements. Both are held to the same
shared conformance vectors (`shared/android/conformance_vectors.json`); a rule
that differs between them fails one side's tests.

`FakeDevice` plugs the reference guard into the real `DeviceHub` through an
in-memory connection, so server tests exercise the real transport — exact
device, no queue, expiry, cancellation, refusals — against a device that
enforces the real rules, with no Android hardware (docs/23 §8).

Every check can only refuse. There is no input that turns a refusal into an
allowance, and nothing a device holds (its grid, its cached app policy) can
make an operation the server did not send happen.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from server.execution.android import DEVICE_MAPPING, MAPPING_VERSION, argument_problem
from shared.schemas.device_channel import (
    MAX_OPERATION_TTL,
    PACKAGE_NAME_PATTERN,
    DeviceRefusalReason,
    GridToggle,
)

# A device clock may run a little ahead of the server's; an envelope issued
# further in the future than this is malformed, not merely early.
MAX_CLOCK_SKEW = timedelta(seconds=60)
_PACKAGE = re.compile(PACKAGE_NAME_PATTERN)


@dataclass
class DeviceLocalState:
    """What the device itself holds: its per-app grid (PRD §13) and the cached
    sensitive-app classification (`GET /devices/app-policy`). Absent policy =
    nothing classified (restrictive)."""

    packages: dict[str, set[str]] = field(default_factory=dict)
    device_state: bool = False
    app_policy: dict[str, list[str]] | None = None
    seen_op_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class GuardVerdict:
    allowed: bool
    reason: DeviceRefusalReason | None = None
    primitive: str | None = None


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("naive timestamp")
    return parsed


def reference_guard(
    envelope: dict[str, Any],
    *,
    device_id: str,
    now: datetime,
    state: DeviceLocalState,
    mapping_version: str = MAPPING_VERSION,
) -> GuardVerdict:
    def refuse(reason: DeviceRefusalReason) -> GuardVerdict:
        return GuardVerdict(False, reason)

    op_id = str(envelope.get("op_id", ""))
    # 1. addressed to this device
    if envelope.get("device_id") != device_id:
        return refuse(DeviceRefusalReason.WRONG_DEVICE)
    # 2. never the same operation twice
    if op_id in state.seen_op_ids:
        return refuse(DeviceRefusalReason.DUPLICATE_OPERATION)
    state.seen_op_ids.add(op_id)
    # 3. a well-formed, bounded window
    try:
        issued = _parse_time(envelope["issued_at"])
        expires = _parse_time(envelope["expires_at"])
    except (KeyError, ValueError, TypeError):
        return refuse(DeviceRefusalReason.MALFORMED_ARGUMENTS)
    if expires <= issued or expires - issued > MAX_OPERATION_TTL or issued > now + MAX_CLOCK_SKEW:
        return refuse(DeviceRefusalReason.MALFORMED_ARGUMENTS)
    # 4. still valid
    if now >= expires:
        return refuse(DeviceRefusalReason.OPERATION_EXPIRED)
    # 5. the same table the server used
    if envelope.get("mapping_version") != mapping_version:
        return refuse(DeviceRefusalReason.MAPPING_VERSION_MISMATCH)
    # 6. a triple in that table
    spec = DEVICE_MAPPING.get(envelope.get("capability", ""), {}).get(envelope.get("operation", ""))
    if spec is None or spec.primitive != envelope.get("primitive"):
        return refuse(DeviceRefusalReason.NOT_IN_MAPPING)
    # 7. exactly the app scope the primitive needs
    package = envelope.get("package_name")
    if spec.package_scope == "required":
        if not isinstance(package, str) or not _PACKAGE.match(package):
            return refuse(DeviceRefusalReason.MALFORMED_ARGUMENTS)
    elif package is not None:
        return refuse(DeviceRefusalReason.MALFORMED_ARGUMENTS)
    # 8. the cached classification — only ever narrower than the server
    policy = state.app_policy or {}
    classified = set(policy.get("non_sensitive", [])) | set(policy.get("sensitive", [])) | set(policy.get("payment", []))
    if spec.grid_toggle is GridToggle.UI_INTERACTION and package not in classified:
        return refuse(DeviceRefusalReason.SENSITIVE_PACKAGE)
    if spec.grid_toggle is GridToggle.SCREENSHOT and package not in set(policy.get("non_sensitive", [])):
        return refuse(DeviceRefusalReason.SENSITIVE_PACKAGE)
    # 9. the user's own per-app grid still allows it
    if spec.grid_toggle is GridToggle.DEVICE_STATE:
        if not state.device_state:
            return refuse(DeviceRefusalReason.TOGGLE_OFF)
    elif spec.grid_toggle.value not in state.packages.get(package or "", set()):
        return refuse(DeviceRefusalReason.TOGGLE_OFF)
    # 10. well-formed arguments
    arguments = envelope.get("arguments")
    if not isinstance(arguments, dict) or argument_problem(spec, arguments) is not None:
        return refuse(DeviceRefusalReason.MALFORMED_ARGUMENTS)
    return GuardVerdict(True, primitive=spec.primitive)


class FakeDevice:
    """A device that runs the reference guard behind the real hub.

    Allowed operations "execute" by answering with a small structured result
    (or with `results[primitive]` when a test supplies one). `hold` makes the
    device take an operation and never answer, for cancellation tests."""

    def __init__(
        self,
        *,
        device_id: uuid.UUID,
        state: DeviceLocalState | None = None,
        clock=lambda: datetime.now(timezone.utc),
        results: dict[str, dict] | None = None,
        hold: bool = False,
    ) -> None:
        self.device_id = device_id
        self.state = state or DeviceLocalState()
        self.clock = clock
        self.results = results or {}
        self.hold = hold
        self.received: list[dict] = []
        self.cancels: list[dict] = []
        self.executed: list[str] = []
        self.session = None
        self.hub = None
        self.closed: tuple[int, str] | None = None

    async def attach(self, hub, *, user_id: uuid.UUID) -> "FakeDevice":
        self.hub = hub
        self.session = await hub.attach(user_id=user_id, device_id=self.device_id, connection=self)
        return self

    # DeviceConnection
    async def send_text(self, text: str) -> None:
        frame = json.loads(text)
        if frame["type"] == "cancel":
            self.cancels.append(frame)
            return
        self.received.append(frame)
        verdict = reference_guard(frame, device_id=str(self.device_id), now=self.clock(), state=self.state)
        if not verdict.allowed:
            reply = {"type": "result", "op_id": frame["op_id"], "status": "refused",
                     "refusal_reason": verdict.reason.value}
        elif self.hold:
            return
        else:
            self.executed.append(verdict.primitive)
            reply = {"type": "result", "op_id": frame["op_id"], "status": "ok",
                     "result": self.results.get(verdict.primitive, {"primitive": verdict.primitive})}
        asyncio.get_running_loop().call_soon(self.hub.deliver, self.session, json.dumps(reply))

    async def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)


__all__ = ["DeviceLocalState", "FakeDevice", "GuardVerdict", "reference_guard"]
