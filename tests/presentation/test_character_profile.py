"""The default character profile (OD-CHAR-1, `docs/DECISION_REGISTER.md` §2M).

Checks the client-neutral contract every client shares: the seven states, the
built-in profile's mapping, fallback for anything unknown, that the approved
originals are present byte for byte (format, dimensions, alpha), and that no
authority package can read a presentation state.
"""

from __future__ import annotations

import ast
import hashlib
import json
import struct
from pathlib import Path

import pytest
from pydantic import ValidationError

from shared.schemas.presentation import (
    CharacterPresentationSettings,
    CharacterProfile,
    CharacterTransition,
    Expression,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = ROOT / "assets" / "character" / "default"

# The owner-supplied originals (2026-10-12): file → (sha256, width, height).
# A changed digest means the artwork was replaced or re-encoded — that needs
# the owner's approval, not a test update.
APPROVED = {
    "default.png": ("8c291af5122c773b08d9870e548325180f2c408f761103c07f4abdc9fb08d89e", 427, 585),
    "satisfied.webp": ("80e3d692b1c54a78a74767474c5f1a3a9d2ba1514646331220eebd2d67d248bc", 1024, 1536),
    "happy.webp": ("5b5e853ed5d6973356dc64b16ccff70c396c59da86092be474db43f3d74c4c54", 1071, 1469),
    "sleepy.webp": ("2ad9003cd0bfd10fea0b1b4ead40db8b2fc7cc2c992c72b7eb1211f9728bc4bd", 1361, 1156),
    "surprised.webp": ("40bd14d8edf0f24c9ff5dd60ec1a01f00311d869104554b3168bc0e2d241a0d6", 1024, 1536),
    "angry.webp": ("95068ad537e9a42167b63a5d610898e453ad49eacbfafa044cf7702ce8c768ae", 1145, 1374),
    "sad.webp": ("468ad50d34fc5e8b097630b5ac3daa1b346d2647bb017d6e14dc4b5481a440d0", 1145, 1374),
}

EXPECTED_MAPPING = {
    Expression.DEFAULT: "default.png",
    Expression.SATISFIED: "satisfied.webp",
    Expression.HAPPY: "happy.webp",
    Expression.SLEEPY: "sleepy.webp",
    Expression.SURPRISED: "surprised.webp",
    Expression.ANGRY: "angry.webp",
    Expression.SAD: "sad.webp",
}


def _profile() -> CharacterProfile:
    return CharacterProfile.model_validate_json((DEFAULT_DIR / "profile.json").read_text())


def _image_header(data: bytes) -> tuple[str, int, int, bool]:
    """(format, width, height, has_alpha) from the file's own header — no decoder."""

    if data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height, _depth, colour = struct.unpack(">IIBB", data[16:26])
        # Colour types 4 (grey+alpha) and 6 (RGBA) carry an alpha channel.
        return "png", width, height, colour in (4, 6)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and data[12:16] == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return "webp", width, height, bool(data[20] & 0x10)
    raise AssertionError("not a PNG or extended WebP")


# --- the states and the built-in profile -----------------------------------


def test_there_are_exactly_seven_states_and_default_is_one_of_them():
    assert [e.value for e in Expression] == [
        "default",
        "satisfied",
        "happy",
        "sleepy",
        "surprised",
        "angry",
        "sad",
    ]


def test_the_built_in_profile_maps_all_seven_states_to_their_images():
    profile = _profile()
    assert profile.profile_id == "default"
    assert profile.fallback is Expression.DEFAULT
    assert profile.states == EXPECTED_MAPPING
    for state, asset in EXPECTED_MAPPING.items():
        assert profile.asset_for(state) == asset
        assert profile.asset_for(state.value) == asset


def test_default_is_selected_when_nothing_is_supplied():
    assert Expression.parse(None) is Expression.DEFAULT
    assert _profile().asset_for(None) == "default.png"


@pytest.mark.parametrize("bad", ["furious", "", " happy", "HAPPY", "Default", 3, None, ["happy"], {"s": 1}])
def test_unknown_or_malformed_states_fall_back_to_default(bad):
    assert Expression.parse(bad) is Expression.DEFAULT
    assert _profile().asset_for(bad) == "default.png"


def test_a_state_the_profile_omits_shows_the_fallback():
    partial = CharacterProfile.model_validate_json(
        json.dumps({"profile_id": "partial", "states": {"default": "default.png", "happy": "happy.webp"}})
    )
    assert partial.asset_for("happy") == "happy.webp"
    assert partial.asset_for("sad") == "default.png"


@pytest.mark.parametrize(
    "doc",
    [
        {"profile_id": "x", "states": {"happy": "happy.webp"}},  # fallback unmapped
        {"profile_id": "x", "states": {"default": "../secrets.png"}},  # a path
        {"profile_id": "x", "states": {"default": "sub/default.png"}},
        {"profile_id": "x", "states": {"default": "default.svg"}},  # not png/webp
        {"profile_id": "../x", "states": {"default": "default.png"}},
        {"profile_id": "x", "states": {"furious": "default.png", "default": "default.png"}},  # unknown state
        {"profile_id": "x", "states": {}},
        {"profile_id": "x", "schema_version": 2, "states": {"default": "default.png"}},
        {"profile_id": "x", "states": {"default": "default.png"}, "mood": "happy"},  # no extra fields
    ],
)
def test_a_malformed_profile_is_rejected(doc):
    with pytest.raises(ValidationError):
        CharacterProfile.model_validate_json(json.dumps(doc))


def test_presentation_settings_are_separate_from_the_mapping():
    settings = CharacterPresentationSettings()
    assert settings.visible is True
    assert settings.profile_id == "default"
    assert settings.transition is CharacterTransition.CROSSFADE
    # Settings name a profile; they never carry a state→image mapping or a state.
    assert set(CharacterPresentationSettings.model_fields) == {"visible", "profile_id", "transition"}
    with pytest.raises(ValidationError):
        CharacterPresentationSettings.model_validate_json(json.dumps({"expression": "happy"}))


# --- the approved artwork ---------------------------------------------------


def test_the_directory_holds_exactly_the_profile_and_the_seven_originals():
    assert {p.name for p in DEFAULT_DIR.iterdir()} == set(APPROVED) | {"profile.json"}


@pytest.mark.parametrize("name", sorted(APPROVED))
def test_each_original_is_unchanged_with_its_format_size_and_alpha(name):
    data = (DEFAULT_DIR / name).read_bytes()
    digest, width, height = APPROVED[name]
    assert hashlib.sha256(data).hexdigest() == digest
    fmt, w, h, alpha = _image_header(data)
    assert fmt == name.rsplit(".", 1)[1], "the extension names the real format"
    assert (w, h) == (width, height)
    assert alpha, "the sticker artwork keeps its transparent background"


# --- the authority boundary -------------------------------------------------

# Everything that authorizes, grants, decides policy or risk, confirms,
# meters, runs the agent, executes or schedules. None may read a presentation
# state (mirrors the import-linter contract of the same name).
AUTHORITY_PACKAGES = [
    "agent",
    "agents",
    "auth",
    "capabilities",
    "execution",
    "fs",
    "gateway",
    "graph",
    "memory",
    "models",
    "modeltools",
    "net",
    "scheduler",
    "secrets",
    "security",
    "tools",
    "vault",
    "voice",
]


def test_no_authority_package_imports_the_presentation_contract():
    offenders = []
    for package in AUTHORITY_PACKAGES:
        for path in (ROOT / "server" / package).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(), str(path))):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
                if any(n.startswith("shared.schemas.presentation") for n in names):
                    offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_the_data_contracts_package_does_not_pull_in_the_presentation_module():
    init = (ROOT / "shared" / "schemas" / "__init__.py").read_text()
    assert "presentation" not in init
