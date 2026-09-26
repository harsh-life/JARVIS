"""shared/schemas/device_channel.py — the device wire contract (docs/23 §4).

Every type is strict (unknown fields rejected) because the Android client is
equally strict: a field one side adds and the other ignores is how the two
halves of two-layer enforcement drift apart without anyone noticing.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from server.execution.android import DeviceTransport
from shared.schemas.device_channel import (
    DeviceCancel,
    DeviceHello,
    DeviceOperationEnvelope,
    DevicePlatformStatus,
    DeviceResultEnvelope,
    DeviceWakePush,
    ScreenNode,
    ScreenReadResult,
)

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def _envelope(**overrides) -> dict:
    payload = {
        "type": "operation",
        "op_id": str(uuid.uuid4()),
        "task_id": str(uuid.uuid4()),
        "device_id": str(uuid.uuid4()),
        "capability": "app.interact",
        "operation": "tap",
        "primitive": "accessibility.tap",
        "package_name": "com.example.app",
        "arguments": {"view_id": "send"},
        "mapping_version": "1-0123456789abcdef",
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(seconds=30)).isoformat(),
    }
    payload.update(overrides)
    return payload


# ── the operation envelope ──────────────────────────────────────────────


def test_a_well_formed_envelope_parses():
    DeviceOperationEnvelope.model_validate(_envelope())


@pytest.mark.parametrize("field", ["authorized", "risk_tier", "confirmed", "user_id", "grant"])
def test_the_envelope_has_no_field_that_could_carry_authority(field):
    """docs/23 §0: the device decides nothing about authority — so there is no
    field in which the server could even try to hand it some."""

    with pytest.raises(ValidationError):
        DeviceOperationEnvelope.model_validate(_envelope(**{field: True}))


def test_an_envelope_whose_window_is_too_long_is_malformed():
    with pytest.raises(ValidationError):
        DeviceOperationEnvelope.model_validate(
            _envelope(expires_at=(NOW + timedelta(seconds=61)).isoformat())
        )


def test_an_envelope_that_expires_before_it_is_issued_is_malformed():
    with pytest.raises(ValidationError):
        DeviceOperationEnvelope.model_validate(_envelope(expires_at=NOW.isoformat()))


@pytest.mark.parametrize("package", ["", "notapackage", "com..x", "com.x;rm", "1com.x"])
def test_package_names_are_shape_checked(package):
    with pytest.raises(ValidationError):
        DeviceOperationEnvelope.model_validate(_envelope(package_name=package))


# ── the result envelope ─────────────────────────────────────────────────


def _result(**fields) -> dict:
    return {"type": "result", "op_id": str(uuid.uuid4()), **fields}


def test_ok_refused_and_failed_are_each_self_consistent():
    DeviceResultEnvelope.model_validate(_result(status="ok", result={"a": 1}, perception_level="accessibility"))
    DeviceResultEnvelope.model_validate(_result(status="refused", refusal_reason="toggle_off"))
    DeviceResultEnvelope.model_validate(_result(status="failed", failure_reason="target_not_found"))


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "refused"},  # no reason
        {"status": "refused", "refusal_reason": "toggle_off", "result": {"leak": "x"}},
        {"status": "ok", "refusal_reason": "toggle_off"},
        {"status": "failed", "failure_reason": "timeout", "result": {"x": 1}},
        {"status": "refused", "refusal_reason": "platform_unavailable"},  # which platform?
        {"status": "refused", "refusal_reason": "toggle_off", "required_platform": "shizuku"},
        {"status": "ok", "result": {}, "extra": 1},
    ],
)
def test_inconsistent_results_are_rejected(fields):
    with pytest.raises(ValidationError):
        DeviceResultEnvelope.model_validate(_result(**fields))


def test_a_platform_refusal_names_the_missing_dependency():
    parsed = DeviceResultEnvelope.model_validate(
        _result(status="refused", refusal_reason="platform_unavailable", required_platform="shizuku")
    )
    assert parsed.required_platform.value == "shizuku"


# ── cancellation ────────────────────────────────────────────────────────


def test_a_cancel_names_exactly_one_target():
    DeviceCancel(op_id=uuid.uuid4())
    DeviceCancel(task_id=uuid.uuid4())
    with pytest.raises(ValidationError):
        DeviceCancel()
    with pytest.raises(ValidationError):
        DeviceCancel(op_id=uuid.uuid4(), task_id=uuid.uuid4())


# ── ANDC-T6 (server half): a password node carrying text is rejected ────


def test_a_redacted_password_node_is_accepted():
    ScreenNode(id=0, role="EditText", bounds=(0, 0, 10, 10), editable=True, password=True)


@pytest.mark.parametrize("field", ["text", "content_description"])
def test_a_password_node_with_any_text_is_rejected(field):
    with pytest.raises(ValidationError):
        ScreenNode(id=0, role="EditText", bounds=(0, 0, 10, 10), password=True, **{field: "hunter2"})


def test_screen_reads_are_bounded():
    node = {"id": 0, "role": "View", "bounds": (0, 0, 1, 1)}
    with pytest.raises(ValidationError):
        ScreenReadResult.model_validate(
            {"app": {"package_name": "com.example"}, "nodes": [node] * 301}
        )


# ── ANDC-T9: the push payload is content-free by construction ───────────


def test_the_wake_push_is_exactly_one_constant_field():
    assert DeviceWakePush().model_dump(mode="json") == {"type": "wake"}
    assert set(DeviceWakePush.model_fields) == {"type"}
    with pytest.raises(ValidationError):
        DeviceWakePush.model_validate({"type": "wake", "op_id": str(uuid.uuid4())})


# ── channel control frames ──────────────────────────────────────────────


def test_hello_carries_credentials_in_the_frame_and_nothing_else():
    DeviceHello(access_token="t", device_proof="p", mapping_version="1-x", client_version="0.1")
    with pytest.raises(ValidationError):
        DeviceHello.model_validate(
            {"type": "hello", "access_token": "t", "device_proof": "p", "mapping_version": "1-x",
             "client_version": "0.1", "user_id": str(uuid.uuid4())}
        )


def test_platform_status_only_names_known_dependencies():
    DevicePlatformStatus(platforms={"shizuku": False})
    with pytest.raises(ValidationError):
        DevicePlatformStatus.model_validate({"type": "platform_status", "platforms": {"root": True}})


# ── ANDC-T1 (signature): connectivity is asked per device, never per user ─


def test_is_connected_is_device_scoped():
    parameters = inspect.signature(DeviceTransport.is_connected).parameters
    assert "device_id" in parameters
    assert "user_id" not in parameters
