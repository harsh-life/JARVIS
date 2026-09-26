"""The shared capability → operation → primitive table (docs/23 §5.1, 08 §2).

One table, two consumers: the server builds envelopes from it, the Android
client refuses anything outside it. These tests pin the properties both halves
rely on — the committed artifact is exactly the table, the table is exactly the
registry's enumeration, and nothing in it is a shell.
"""

from __future__ import annotations

import json
import re
import uuid

import pytest

from server.capabilities.registry import CAPABILITY_REGISTRY
from server.execution.android import (
    DEVICE_MAPPING,
    MAPPING_ARTIFACT_PATH,
    MAPPING_VERSION,
    argument_problem,
    build_operation,
    canonical_json,
    check_artifact,
    export_mapping,
    mapping_document,
    primitive_spec,
)
from shared.schemas.device_channel import DeviceMechanism, DevicePlatformDependency
from shared.schemas.execution import ExecutionError, ExecutionErrorCode


# ── drift: the committed artifact is the table ──────────────────────────


def test_the_committed_artifact_is_exactly_the_exported_table():
    """ANDC-T4's precondition. If this fails: `python -m
    server.execution.export_device_mapping export`, and ship a client built
    against the new file — an old client then refuses on version mismatch."""

    assert check_artifact(), "shared/android/device_mapping.json is stale"


def test_the_artifact_carries_its_own_version_and_it_is_the_content_digest():
    committed = json.loads(MAPPING_ARTIFACT_PATH.read_text(encoding="ascii"))
    assert committed["mapping_version"] == MAPPING_VERSION
    body = dict(committed)
    del body["mapping_version"]
    assert body == mapping_document()
    assert re.fullmatch(r"\d+-[0-9a-f]{16}", MAPPING_VERSION)


def test_any_content_change_changes_the_version():
    document = mapping_document()
    document["capabilities"]["device.read"]["read_battery"]["max_result_bytes"] += 1
    import hashlib

    digest = hashlib.sha256(canonical_json(document).encode("ascii")).hexdigest()[:16]
    assert not MAPPING_VERSION.endswith(digest)


def test_the_artifact_is_ascii_so_both_canonicalizers_agree():
    """The Android contract module recomputes the digest with its own
    canonical serializer; ASCII-only content keeps that byte-exact."""

    MAPPING_ARTIFACT_PATH.read_text(encoding="ascii")
    assert export_mapping().isascii()


# ── AND-T1: the table is the registry's enumeration, nothing more ───────


@pytest.mark.parametrize("capability", sorted(DEVICE_MAPPING))
def test_mapped_operations_equal_the_registrys_enumerated_set(capability):
    assert set(DEVICE_MAPPING[capability]) == set(CAPABILITY_REGISTRY[capability].operations)


def test_only_device_capabilities_are_mapped():
    assert set(DEVICE_MAPPING) == {"app.interact", "device.read", "device.ui_control"}


# ── AND-T7: no shell, typed Shizuku only ────────────────────────────────


_SHELLISH = re.compile(r"shell|exec|argv|command|(^|[._])(cmd|sh|su|adb)([._]|$)", re.IGNORECASE)


def test_no_primitive_or_argument_is_shell_shaped():
    for capability, operations in DEVICE_MAPPING.items():
        for operation, spec in operations.items():
            assert not _SHELLISH.search(spec.primitive), spec.primitive
            for name in spec.arguments:
                assert not _SHELLISH.search(name), (capability, operation, name)


def test_every_shizuku_primitive_is_listed_explicitly_and_depends_on_shizuku():
    shizuku = [
        (c, o, s) for c, ops in DEVICE_MAPPING.items() for o, s in ops.items()
        if s.mechanism is DeviceMechanism.SHIZUKU
    ]
    assert [(c, o) for c, o, _ in shizuku] == [("app.interact", "force_stop")]
    for _, _, spec in shizuku:
        assert spec.primitive.startswith("shizuku.")
        assert DevicePlatformDependency.SHIZUKU in spec.dependencies
        # A typed call: no free-form argument of any kind.
        assert spec.arguments == {}


def test_primitive_names_match_their_mechanism():
    prefixes = {
        DeviceMechanism.ACCESSIBILITY: "accessibility.",
        DeviceMechanism.ANDROID_API: "android.",
        DeviceMechanism.SHIZUKU: "shizuku.",
    }
    for operations in DEVICE_MAPPING.values():
        for spec in operations.values():
            assert spec.primitive.startswith(prefixes[spec.mechanism]), spec.primitive
            # Only Shizuku primitives may need Shizuku (on-demand: nothing else
            # ever waits on it).
            needs_shizuku = DevicePlatformDependency.SHIZUKU in spec.dependencies
            assert needs_shizuku == (spec.mechanism is DeviceMechanism.SHIZUKU)


def test_screenshot_is_its_own_operation_never_part_of_read_screen():
    read_screen = primitive_spec("device.read", "read_screen")
    screenshot = primitive_spec("device.read", "capture_screenshot")
    assert read_screen.primitive != screenshot.primitive
    assert DevicePlatformDependency.SCREEN_CAPTURE not in read_screen.dependencies
    assert screenshot.grid_toggle.value == "screenshot"
    # Only the screenshot may return a result large enough to hold an image.
    others = [
        s.max_result_bytes for ops in DEVICE_MAPPING.values() for s in ops.values()
        if s.primitive != screenshot.primitive
    ]
    assert max(others) < screenshot.max_result_bytes


def test_ui_acting_operations_are_bound_to_one_named_app():
    for capability in ("app.interact", "device.ui_control"):
        for operation, spec in DEVICE_MAPPING[capability].items():
            assert spec.package_scope == "required", (capability, operation)


def test_ui_targets_are_never_raw_coordinates():
    """08 §7: 'it does not blindly tap coordinates'."""

    for operations in DEVICE_MAPPING.values():
        for spec in operations.values():
            assert not {"x", "y"} & set(spec.arguments)


# ── argument validation (AND-T6) ─────────────────────────────────────────


@pytest.mark.parametrize(
    "arguments, ok",
    [
        ({"view_id": "send"}, True),
        ({"text": "Send", "index": 2}, True),
        ({"content_description": "Send"}, True),
        ({}, False),  # no target
        ({"view_id": "send", "text": "Send"}, False),  # two targets
        ({"view_id": ""}, False),
        ({"view_id": "x" * 201}, False),
        ({"view_id": 7}, False),
        ({"view_id": "send", "index": -1}, False),
        ({"view_id": "send", "index": 51}, False),
        ({"view_id": "send", "index": True}, False),  # bool is not an int
        ({"view_id": "send", "x": 1}, False),  # unknown key
    ],
)
def test_tap_arguments(arguments, ok):
    assert (argument_problem(primitive_spec("app.interact", "tap"), arguments) is None) is ok


def test_enum_arguments_are_closed():
    spec = primitive_spec("device.ui_control", "global_action")
    assert argument_problem(spec, {"action": "back"}) is None
    assert argument_problem(spec, {"action": "power_off"}) is not None
    assert argument_problem(spec, {}) is not None


def test_malformed_arguments_are_refused_before_any_transport():
    with pytest.raises(ExecutionError) as excinfo:
        build_operation(
            capability="app.interact", operation="input_text", package_name="com.example",
            arguments={"text": "hi", "shell": "rm -rf /"}, user_id=uuid.uuid4(),
            task_id=uuid.uuid4(), device_id=uuid.uuid4(),
        )
    assert excinfo.value.code is ExecutionErrorCode.INVALID_ARGUMENTS


# ── fresh identity and a short window on every build ────────────────────


def test_every_build_mints_a_fresh_op_id_and_a_short_window():
    kwargs = dict(
        capability="device.read", operation="read_battery", package_name=None, arguments={},
        user_id=uuid.uuid4(), task_id=uuid.uuid4(), device_id=uuid.uuid4(),
    )
    first, second = build_operation(**kwargs), build_operation(**kwargs)
    assert first.op_id != second.op_id
    assert (first.expires_at - first.issued_at).total_seconds() == 30
    assert first.mapping_version == MAPPING_VERSION


def test_a_device_level_read_never_carries_a_package():
    op = build_operation(
        capability="device.read", operation="read_battery", package_name="com.bank",
        arguments={}, user_id=uuid.uuid4(), task_id=uuid.uuid4(), device_id=uuid.uuid4(),
    )
    assert op.package_name is None


def test_the_envelope_omits_the_server_side_user_id():
    op = build_operation(
        capability="device.read", operation="read_screen", package_name="com.example",
        arguments={}, user_id=uuid.uuid4(), task_id=uuid.uuid4(), device_id=uuid.uuid4(),
    )
    wire = op.envelope().model_dump(mode="json")
    assert "user_id" not in wire
    assert wire["device_id"] == str(op.device_id)
    assert wire["mapping_version"] == MAPPING_VERSION


@pytest.mark.parametrize("package", ["com..notes", "notapackage", "com.x;rm -rf", "1com.x"])
def test_a_malformed_package_is_a_typed_refusal_not_a_crash(package):
    """Found by the shared conformance vectors: a malformed package must be a
    typed `invalid_arguments` refusal at build time, never a raw validation
    error escaping the adapter when the envelope is serialized."""

    with pytest.raises(ExecutionError) as excinfo:
        build_operation(
            capability="app.interact", operation="tap", package_name=package,
            arguments={"view_id": "send"}, user_id=uuid.uuid4(), task_id=uuid.uuid4(),
            device_id=uuid.uuid4(),
        )
    assert excinfo.value.code is ExecutionErrorCode.INVALID_ARGUMENTS
