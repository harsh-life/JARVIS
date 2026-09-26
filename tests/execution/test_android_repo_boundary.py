"""REPO-T2 / REPO-T3 for `android/` (16 §3–§4, docs/23 §8).

REPO-T2: no server secret exists in `android/` (and, in the Android CI job,
none in the built APK — `tests/tools/check_apk_secrets.py`, run by CI). REPO-T3:
`android/` and `server/` share only `shared/` — the wire contract and the
versioned mapping artifact. Also pinned here: the donor architecture that
docs/23 §1 says is not restored stays absent (the on-device model router, the
raw-ADB allow-list gate, and the reflective generic Shizuku process spawn).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from server.security.secret_patterns import find_secret

REPO = Path(__file__).resolve().parents[2]
ANDROID = REPO / "android"

_TEXT_SUFFIXES = {".kt", ".kts", ".xml", ".json", ".properties", ".toml", ".pro", ".yml", ".md", ".py"}


def _tracked_android_files() -> list[Path]:
    """Files git would commit under android/ (tracked or not yet added, minus
    ignored) — build outputs and local.properties never count."""

    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "android"],
        cwd=REPO, check=True, capture_output=True, text=True,
    ).stdout
    return [REPO / line for line in out.splitlines() if line]


def _text_files() -> list[Path]:
    return [p for p in _tracked_android_files() if p.suffix in _TEXT_SUFFIXES and p.exists()]


def test_the_android_project_exists_and_is_scanned():
    files = _text_files()
    assert any(p.name == "settings.gradle.kts" for p in files)
    assert any(p.suffix == ".kt" for p in files)


# ── REPO-T2 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", _text_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_no_secret_shaped_value_is_committed_under_android(path: Path):
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        hit = find_secret(line)
        # `credential_assignment` matches source code such as
        # `val password: Boolean` (a redaction flag); every key-format and
        # high-entropy detector still applies to every line.
        if hit is not None and hit != "credential_assignment":
            pytest.fail(f"{path.relative_to(REPO)}:{number} looks like a secret ({hit})")


def test_no_build_config_field_can_carry_a_secret():
    """The donor compiled a Groq API key into BuildConfig — the exact failure
    REPO-T2 exists to catch. No `buildConfigField` is allowed at all."""

    for path in _text_files():
        if path.suffix == ".kts":
            assert "buildConfigField" not in path.read_text(encoding="utf-8"), path


def test_no_local_credentials_file_is_committed():
    names = {p.name for p in _tracked_android_files()}
    for forbidden in ("local.properties", "google-services.json", "keystore.properties"):
        assert forbidden not in names
    assert not any(p.suffix in {".jks", ".keystore", ".p12"} for p in _tracked_android_files())


# ── REPO-T3 ──────────────────────────────────────────────────────────────


def test_the_build_reaches_outside_android_only_for_shared_android():
    outside = re.compile(r"\.\./[A-Za-z_./-]+")
    for path in _text_files():
        if path.suffix != ".kts":
            continue
        for reference in outside.findall(path.read_text(encoding="utf-8")):
            assert reference.startswith("../shared/android"), (path, reference)


def test_no_build_configuration_pulls_in_server_code():
    """Kotlin cannot import the Python server; the way server code could leak
    into the APK is a build script adding it as a source or asset directory."""

    server_ref = re.compile(r"\bserver[./]")
    for path in _text_files():
        if path.suffix in {".kts", ".pro", ".toml", ".properties"}:
            assert not server_ref.search(path.read_text(encoding="utf-8")), path


# ── docs/23 §1: the old HyperMind architecture is not restored ──────────


@pytest.mark.parametrize(
    "pattern",
    [
        r"(?i)groq",  # on-device model routing
        r"api\.groq\.com|openai|anthropic|chat/completions",
        r"(?i)allowlist\.snapshot|OfflineGate|hard_deny",  # raw-ADB allow-list gate
        r"newProcess|getDeclaredMethod",  # reflective generic Shizuku spawn
        r"Runtime\.getRuntime\(\)\.exec|ProcessBuilder",  # any shell from the app process
    ],
)
def test_donor_architecture_is_absent(pattern):
    compiled = re.compile(pattern)
    for path in _text_files():
        if path.suffix == ".kt" or path.name == "AndroidManifest.xml":
            assert not compiled.search(path.read_text(encoding="utf-8")), (path, pattern)
