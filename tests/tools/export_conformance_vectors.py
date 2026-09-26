"""`python -m tests.tools.export_conformance_vectors [export|check]`.

Writes `shared/android/conformance_vectors.json` — docs/23 §9's shared suite of
`(envelope, local grid state, expected outcome)` cases. The server runs them
through the reference guard and the fake device behind the real hub
(tests/execution/test_device_conformance.py); the Android client runs them
through its `DeviceGuard` (android/contract ConformanceVectorTest). Each case
names the acceptance hook it covers.

Regenerate after any mapping change (the vectors embed the mapping version).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from server.execution.android import MAPPING_VERSION

PATH = Path(__file__).resolve().parents[2] / "shared" / "android" / "conformance_vectors.json"

DEVICE = "6f1c2d3e-4a5b-4c6d-8e7f-00000000d001"
OTHER_DEVICE = "6f1c2d3e-4a5b-4c6d-8e7f-00000000d002"
TASK = "6f1c2d3e-4a5b-4c6d-8e7f-00000000a001"
NOW = "2026-09-26T12:00:10Z"
ISSUED = "2026-09-26T12:00:00Z"
EXPIRES = "2026-09-26T12:00:30Z"

NOTES = "com.example.notes"
OTHER = "com.example.other"
BANK = "com.bank.app"
WALLET = "com.wallet.pay"
UNKNOWN = "com.unknown.app"

DEFAULT_STATE = {
    "grid": {
        "packages": {NOTES: ["screen_read", "ui_interaction", "screenshot"], BANK: ["screen_read", "ui_interaction", "screenshot"],
                     UNKNOWN: ["screen_read", "ui_interaction", "screenshot"]},
        "device_state": True,
    },
    "app_policy": {"non_sensitive": [NOTES, OTHER], "sensitive": [BANK], "payment": [WALLET]},
    "previously_seen": [],
}


def _op(n: int) -> str:
    return f"6f1c2d3e-4a5b-4c6d-8e7f-{n:012d}"


def envelope(n: int, capability: str, operation: str, primitive: str, package: str | None, arguments=None, **over):
    body = {
        "type": "operation", "op_id": _op(n), "task_id": TASK, "device_id": DEVICE,
        "capability": capability, "operation": operation, "primitive": primitive,
        "package_name": package, "arguments": arguments or {},
        "mapping_version": MAPPING_VERSION, "issued_at": ISSUED, "expires_at": EXPIRES,
    }
    body.update(over)
    return body


def case(name: str, hooks: list[str], env: dict, expected: str, **state) -> dict:
    entry = {"name": name, "hooks": hooks, "envelope": env,
             "expected": {"outcome": "allowed"} if expected == "allowed" else {"outcome": "refused", "reason": expected}}
    if state:
        entry["state"] = state
    return entry


def cases() -> list[dict]:
    tap = ("app.interact", "tap", "accessibility.tap")
    return [
        case("read_battery_allowed", ["AND-T1"],
             envelope(1, "device.read", "read_battery", "android.api.battery_state", None), "allowed"),
        case("tap_in_non_sensitive_app_allowed", ["AND-T1"],
             envelope(2, *tap, NOTES, {"view_id": "send"}), "allowed"),
        case("read_screen_allowed", ["AND-T1"],
             envelope(3, "device.read", "read_screen", "accessibility.read_tree", NOTES), "allowed"),
        case("screenshot_of_non_sensitive_app_allowed", ["ANDC-T7"],
             envelope(4, "device.read", "capture_screenshot", "accessibility.screenshot", NOTES), "allowed"),
        case("tap_in_sensitive_app_passes_the_guard", ["ANDC-T5"],
             envelope(5, *tap, BANK, {"text": "Send"}), "allowed"),
        case("typed_shizuku_primitive_passes_the_guard", ["AND-T7"],
             envelope(6, "app.interact", "force_stop", "shizuku.force_stop_package", NOTES), "allowed"),
        case("wrong_device_refused", ["ANDC-T1", "ANDC-T3"],
             envelope(7, *tap, NOTES, {"view_id": "send"}, device_id=OTHER_DEVICE), "wrong_device"),
        case("expired_refused", ["ANDC-T3"],
             envelope(8, *tap, NOTES, {"view_id": "send"}, issued_at="2026-09-26T11:59:40Z",
                      expires_at="2026-09-26T12:00:05Z"), "operation_expired"),
        case("window_longer_than_ceiling_refused", ["ANDC-T3"],
             envelope(9, *tap, NOTES, {"view_id": "send"}, expires_at="2026-09-26T12:01:30Z"), "malformed_arguments"),
        case("future_dated_envelope_refused", ["ANDC-T3"],
             envelope(10, *tap, NOTES, {"view_id": "send"}, issued_at="2026-09-26T12:05:00Z",
                      expires_at="2026-09-26T12:05:30Z"), "malformed_arguments"),
        case("mapping_version_mismatch_refused", ["ANDC-T4"],
             envelope(11, *tap, NOTES, {"view_id": "send"}, mapping_version="1-0000000000000000"),
             "mapping_version_mismatch"),
        case("unenumerated_operation_refused", ["AND-T1"],
             envelope(12, "app.interact", "delete_app", "accessibility.tap", NOTES), "not_in_mapping"),
        case("primitive_substitution_refused", ["AND-T1"],
             envelope(13, "app.interact", "tap", "shizuku.force_stop_package", NOTES, {"view_id": "send"}),
             "not_in_mapping"),
        case("system_restricted_shell_refused", ["AND-T7"],
             envelope(14, "system.restricted", "run_shell_command", "shizuku.elevated_shell", None,
                      {"argv": ["sh", "-c", "id"]}), "not_in_mapping"),
        case("ui_toggle_off_refused", ["ANDC-T5", "AND-T5"],
             envelope(15, *tap, OTHER, {"view_id": "send"}), "toggle_off"),
        case("device_state_toggle_off_refused", ["ANDC-T5", "AND-T5"],
             envelope(16, "device.read", "read_battery", "android.api.battery_state", None), "toggle_off",
             grid={"packages": {}, "device_state": False}),
        case("screenshot_toggle_off_refused", ["ANDC-T5", "ANDC-T7"],
             envelope(17, "device.read", "capture_screenshot", "accessibility.screenshot", NOTES), "toggle_off",
             grid={"packages": {NOTES: ["screen_read", "ui_interaction"]}, "device_state": True}),
        case("ui_in_unclassified_app_refused", ["ANDC-T5"],
             envelope(18, *tap, UNKNOWN, {"view_id": "send"}), "sensitive_package"),
        case("screenshot_of_sensitive_app_refused", ["ANDC-T7"],
             envelope(19, "device.read", "capture_screenshot", "accessibility.screenshot", BANK), "sensitive_package"),
        case("no_cached_policy_means_restrictive", ["ANDC-T5"],
             envelope(20, *tap, NOTES, {"view_id": "send"}), "sensitive_package", app_policy=None),
        case("missing_required_package_refused", ["AND-T6"],
             envelope(21, *tap, None, {"view_id": "send"}), "malformed_arguments"),
        case("package_on_device_level_read_refused", ["AND-T6"],
             envelope(22, "device.read", "read_battery", "android.api.battery_state", NOTES), "malformed_arguments"),
        case("malformed_package_name_refused", ["AND-T6"],
             envelope(23, *tap, "com..notes", {"view_id": "send"}), "malformed_arguments"),
        case("unknown_argument_refused", ["AND-T6"],
             envelope(24, *tap, NOTES, {"view_id": "send", "x": 10}), "malformed_arguments"),
        case("two_targets_refused", ["AND-T6"],
             envelope(25, *tap, NOTES, {"view_id": "send", "text": "Send"}), "malformed_arguments"),
        case("wrongly_typed_argument_refused", ["AND-T6"],
             envelope(26, *tap, NOTES, {"view_id": "send", "index": "1"}), "malformed_arguments"),
        case("closed_enum_refused", ["AND-T6"],
             envelope(27, "device.ui_control", "global_action", "accessibility.global_action", NOTES,
                      {"action": "power_off"}), "malformed_arguments"),
        case("argument_on_argumentless_shizuku_primitive_refused", ["AND-T6", "AND-T7"],
             envelope(28, "app.interact", "force_stop", "shizuku.force_stop_package", NOTES, {"argv": ["am"]}),
             "malformed_arguments"),
        case("replayed_operation_refused", ["ANDC-T8"],
             envelope(29, *tap, NOTES, {"view_id": "send"}), "duplicate_operation", previously_seen=[_op(29)]),
    ]


def document() -> dict:
    return {
        "mapping_version": MAPPING_VERSION,
        "device_id": DEVICE,
        "now": NOW,
        "default_state": DEFAULT_STATE,
        "cases": cases(),
    }


def render() -> str:
    return json.dumps(document(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "check"
    if command == "export":
        PATH.write_text(render(), encoding="ascii")
        print(f"wrote {PATH} ({len(cases())} cases)")
        return 0
    ok = PATH.exists() and PATH.read_text(encoding="ascii") == render()
    print("conformance vectors up to date" if ok else "conformance vectors stale — run export")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
