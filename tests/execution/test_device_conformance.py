"""The shared device conformance vectors (docs/23 §9), server side.

Two layers of two-layer enforcement (08 §4), against one set of cases:

* the reference device guard (the rules the Android `DeviceGuard` must match —
  the same file is run by android/contract's ConformanceVectorTest), and
* the server-side fake transport: every case the server can produce is sent
  through `build_operation` and the real `DeviceHub` to a fake device running
  that guard. A case the server itself refuses (unmapped, malformed) never
  reaches the device at all — the first layer.
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from datetime import datetime

import pytest

from server.execution.android import build_operation
from server.execution.device_hub import DeviceHub
from shared.schemas.device_channel import DeviceRefusalReason
from shared.schemas.execution import ExecutionError, ExecutionErrorCode
from tests.fake_device import DeviceLocalState, FakeDevice, reference_guard
from tests.tools.export_conformance_vectors import PATH, render

VECTORS = json.loads(PATH.read_text(encoding="ascii"))
CASES = VECTORS["cases"]
USER = uuid.uuid4()


def _state(case: dict) -> DeviceLocalState:
    merged = copy.deepcopy(VECTORS["default_state"])
    merged.update(copy.deepcopy(case.get("state", {})))
    return DeviceLocalState(
        packages={pkg: set(toggles) for pkg, toggles in merged["grid"]["packages"].items()},
        device_state=merged["grid"]["device_state"],
        app_policy=merged["app_policy"],
        seen_op_ids=set(merged["previously_seen"]),
    )


def test_the_committed_vectors_are_current():
    """Regenerate with `python -m tests.tools.export_conformance_vectors export`
    after a mapping change — they embed the mapping version."""

    assert PATH.read_text(encoding="ascii") == render()


def test_every_device_acceptance_hook_has_vectors():
    hooks = {hook for case in CASES for hook in case["hooks"]}
    for required in ("ANDC-T1", "ANDC-T3", "ANDC-T4", "ANDC-T5", "ANDC-T7", "ANDC-T8",
                     "AND-T1", "AND-T5", "AND-T6", "AND-T7"):
        assert required in hooks, required
    # Both outcomes are exercised, and every refusal reason the guard can give.
    reasons = {c["expected"].get("reason") for c in CASES}
    assert {r.value for r in (
        DeviceRefusalReason.WRONG_DEVICE, DeviceRefusalReason.OPERATION_EXPIRED,
        DeviceRefusalReason.MAPPING_VERSION_MISMATCH, DeviceRefusalReason.NOT_IN_MAPPING,
        DeviceRefusalReason.TOGGLE_OFF, DeviceRefusalReason.MALFORMED_ARGUMENTS,
        DeviceRefusalReason.SENSITIVE_PACKAGE, DeviceRefusalReason.DUPLICATE_OPERATION,
    )} <= reasons
    assert any(c["expected"]["outcome"] == "allowed" for c in CASES)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_the_reference_guard_matches_every_vector(case):
    verdict = reference_guard(
        case["envelope"],
        device_id=VECTORS["device_id"],
        now=datetime.fromisoformat(VECTORS["now"].replace("Z", "+00:00")),
        state=_state(case),
    )
    if case["expected"]["outcome"] == "allowed":
        assert verdict.allowed, verdict
    else:
        assert not verdict.allowed
        assert verdict.reason.value == case["expected"]["reason"]


_ENVELOPE_ONLY_FIELDS = ("device_id", "mapping_version", "primitive", "issued_at", "expires_at")
_REFUSAL_CODE = {"operation_expired": ExecutionErrorCode.OPERATION_EXPIRED}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
async def test_the_fake_transport_enforces_every_server_producible_vector(case):
    env = case["envelope"]
    device_id = uuid.UUID(VECTORS["device_id"])
    try:
        operation = build_operation(
            capability=env["capability"], operation=env["operation"], package_name=env["package_name"],
            arguments=env["arguments"], user_id=USER, task_id=uuid.UUID(env["task_id"]), device_id=device_id,
        )
    except ExecutionError:
        # Layer 1: the server refuses before any transport — so nothing an
        # unmapped or malformed operation carries can reach a device.
        assert case["expected"]["outcome"] == "refused"
        return

    produced = operation.envelope().model_dump(mode="json")
    if any(produced[f] != env[f] for f in ("device_id", "mapping_version", "primitive")) or (
        env["issued_at"], env["expires_at"]) != ("2026-09-26T12:00:00Z", "2026-09-26T12:00:30Z") or (
        produced["package_name"] != env["package_name"]
    ):
        pytest.skip("an envelope the server cannot produce — covered by the reference-guard vector")

    hub = DeviceHub()
    state = _state(case)
    # The server mints a fresh op id; a "previously seen" case replays it.
    if case["expected"].get("reason") == "duplicate_operation":
        state.seen_op_ids.add(str(operation.op_id))
    device = await FakeDevice(device_id=device_id, state=state).attach(hub, user_id=USER)
    if case["expected"]["outcome"] == "allowed":
        if operation.primitive == "accessibility.screenshot":
            # The device took it; with no vision rung configured the server
            # drops the image rather than rendering it (device_observations).
            with pytest.raises(ExecutionError) as exc:
                await asyncio.wait_for(hub.send(operation), 5)
            assert exc.value.code is ExecutionErrorCode.PLATFORM_UNSUPPORTED
            assert device.executed == [operation.primitive]
            return
        result = await asyncio.wait_for(hub.send(operation), 5)
        assert device.executed == [operation.primitive]
        assert result.metadata["primitive"] == operation.primitive
    else:
        with pytest.raises(ExecutionError) as exc:
            await asyncio.wait_for(hub.send(operation), 5)
        reason = case["expected"]["reason"]
        assert exc.value.code is _REFUSAL_CODE.get(reason, ExecutionErrorCode.DEVICE_REFUSED)
        assert reason in str(exc.value)
        assert device.executed == []


async def test_the_fake_device_takes_nothing_while_disconnected():
    """ANDC-T2 against the fake transport: no queue."""

    hub = DeviceHub()
    device_id = uuid.uuid4()
    operation = build_operation(capability="device.read", operation="read_battery", package_name=None,
                                arguments={}, user_id=USER, task_id=uuid.uuid4(), device_id=device_id)
    with pytest.raises(ExecutionError) as exc:
        await hub.send(operation)
    assert exc.value.code is ExecutionErrorCode.DEVICE_UNAVAILABLE
    device = await FakeDevice(device_id=device_id, state=DeviceLocalState(device_state=True)).attach(hub, user_id=USER)
    await asyncio.sleep(0.05)
    assert device.received == []


async def test_a_held_operation_is_cancelled_on_the_fake_device():
    """ANDC-T8 against the fake transport."""

    hub = DeviceHub()
    device_id = uuid.uuid4()
    device = await FakeDevice(device_id=device_id, state=DeviceLocalState(device_state=True), hold=True).attach(
        hub, user_id=USER)
    task_id = uuid.uuid4()
    operation = build_operation(capability="device.read", operation="read_battery", package_name=None,
                                arguments={}, user_id=USER, task_id=task_id, device_id=device_id)
    sending = asyncio.ensure_future(hub.send(operation))
    await asyncio.sleep(0.05)
    hub.cancel_task(task_id)
    with pytest.raises(ExecutionError) as exc:
        await sending
    assert exc.value.code is ExecutionErrorCode.CANCELLED
    await asyncio.sleep(0.05)
    assert device.cancels == [{"type": "cancel", "task_id": str(task_id)}]
