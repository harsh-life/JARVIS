"""Text I/O never depends on the host's locale (cross-platform portability).

`Path.read_text()` / `open()` without `encoding=` decode with the platform's
locale encoding — UTF-8 on a typical Linux or macOS host, cp1252 on Windows —
and text-mode writes translate `\\n` to the platform's line ending. Every text
file this repository reads or writes is UTF-8 with LF line endings, and several
are hashed or compared against a pinned copy (the annotation specs, the frozen
baseline prompt), so the default is a latent defect on any host whose locale
is not UTF-8: Windows test runs failed exactly this way (`—` read as `â€”`, a
spec file hashed with CRLF endings).

Two layers of protection:

* a structural guard — every `read_text`/`write_text`/text-mode `open` and
  text-mode `subprocess` call in `server/`, `shared/`, `experiments/` and
  `tests/` names its encoding, and every text write names its newline;
* behavioural checks with non-ASCII content (an em dash, an ellipsis, an
  arrow), including a child interpreter whose locale encoding is deliberately
  not UTF-8 where the host can provide one.
"""

from __future__ import annotations

import ast
import hashlib
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from server.config.errors import ConfigError
from server.config.loader import load_config

REPO_ROOT = Path(__file__).parents[2]
SCANNED = ("server", "shared", "experiments", "tests")
# Modules whose `.open` is binary by construction (archives, images, raw fds).
BINARY_OPENERS = {"tarfile", "zipfile", "os", "gzip", "bz2", "lzma", "image_module", "Image"}
NON_ASCII = "Policy — not you… a → b"


def _text_io_without_encoding(path: Path) -> list[str]:
    problems = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        keywords = {k.arg for k in node.keywords}
        if None in keywords:  # **kwargs: cannot be judged statically
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        where = f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
        if name == "read_text" and "encoding" not in keywords:
            problems.append(f"{where}: read_text() without encoding=")
        elif name == "write_text" and not {"encoding", "newline"} <= keywords:
            missing = " and ".join(f"{k}=" for k in ("encoding", "newline") if k not in keywords)
            problems.append(f"{where}: write_text() without {missing}")
        elif (name in ("run", "check_output", "Popen") and isinstance(func, ast.Attribute)
              and isinstance(func.value, ast.Name) and func.value.id == "subprocess"
              and keywords & {"text", "universal_newlines"} and "encoding" not in keywords):
            problems.append(f"{where}: text-mode subprocess.{name}() without encoding=")
        elif name == "open":
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in BINARY_OPENERS:
                continue
            builtin = isinstance(func, ast.Name)
            mode = node.args[1] if builtin and len(node.args) > 1 else node.args[0] if not builtin and node.args else None
            for keyword in node.keywords:
                if keyword.arg == "mode":
                    mode = keyword.value
            if mode is not None and not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)):
                continue
            text_mode = "r" if mode is None else mode.value
            if "b" in text_mode:
                continue
            if "encoding" not in keywords:
                problems.append(f"{where}: text-mode open() without encoding=")
            elif any(c in text_mode for c in "wax") and "newline" not in keywords:
                problems.append(f"{where}: text-mode write open() without newline=")
    return problems


def _frozen_code() -> set[Path]:
    """Evaluation code pinned byte-for-byte by the phase-04 rules freeze
    (experiments/annotation/phase04_rules_freeze.json). Editing it — even to
    add an encoding — invalidates the freeze, so it is exempt here; making it
    portable needs a new freeze, a research decision, not a portability fix."""

    from experiments.annotation_tools import FROZEN_CODE

    return {REPO_ROOT / path for path in FROZEN_CODE}


def test_every_text_read_and_write_names_its_encoding_and_newline() -> None:
    frozen = _frozen_code()
    problems = [
        problem
        for root in SCANNED
        for path in sorted((REPO_ROOT / root).rglob("*.py"))
        if path not in frozen
        for problem in _text_io_without_encoding(path)
    ]
    assert problems == [], "locale-dependent text I/O:\n" + "\n".join(problems)


def test_frozen_evaluation_code_still_matches_its_rules_freeze() -> None:
    # The exemption above is only safe if the frozen files really are untouched.
    import json

    from experiments.annotation_tools import RULES_FREEZE

    pinned = json.loads(RULES_FREEZE.read_text(encoding="utf-8"))["code"]
    assert set(pinned) == {p.relative_to(REPO_ROOT).as_posix() for p in _frozen_code()}
    for path, digest in pinned.items():
        assert hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest() == digest, path


def test_the_guard_itself_catches_each_form(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(textwrap.dedent("""
        from pathlib import Path
        Path("a").read_text()
        Path("a").write_text("x", encoding="utf-8")
        open("a")
        open("a", "w", encoding="utf-8")
        Path("a").open("a")
        open("a", "rb")
        Path("a").read_text(encoding="utf-8")
        import subprocess
        subprocess.run(["x"], capture_output=True, text=True)
        subprocess.run(["x"], capture_output=True, text=True, encoding="utf-8")
    """), encoding="utf-8", newline="\n")
    original = REPO_ROOT
    try:
        globals()["REPO_ROOT"] = tmp_path
        found = _text_io_without_encoding(sample)
    finally:
        globals()["REPO_ROOT"] = original
    assert [line.split(": ", 1)[1] for line in found] == [
        "read_text() without encoding=",
        "write_text() without newline=",
        "text-mode open() without encoding=",
        "text-mode write open() without newline=",
        "text-mode open() without encoding=",
        "text-mode subprocess.run() without encoding=",
    ]


# ── configuration ────────────────────────────────────────────────────────────


def _config_with_non_ascii(tmp_path: Path) -> Path:
    example = (REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8")
    path = tmp_path / "config.yaml"
    path.write_text(f"# operator note: {NON_ASCII}\n{example}", encoding="utf-8", newline="\n")
    return path


def test_config_with_non_ascii_text_loads(tmp_path: Path) -> None:
    load_config(_config_with_non_ascii(tmp_path))


def test_config_that_is_not_utf8_is_a_config_error_not_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_bytes("# caf\u00e9\n".encode("latin-1") + (REPO_ROOT / "config.example.yaml").read_bytes())
    with pytest.raises(ConfigError, match="not valid UTF-8"):
        load_config(path)


def _non_utf8_locale_env() -> dict[str, str] | None:
    """An environment whose child interpreter decodes with a non-UTF-8 locale
    encoding, or None where the host cannot provide one (macOS always reports
    UTF-8). Windows' default (cp1252) already is one; on Linux the C locale is,
    once Python's own UTF-8 coercion (PEP 538/540) is switched off."""

    env = {k: v for k, v in os.environ.items() if not k.startswith(("LC_", "PYTHONUTF8", "PYTHONIOENCODING"))}
    env.update(LANG="C", LC_ALL="C", PYTHONCOERCECLOCALE="0", PYTHONUTF8="0")
    probe = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c", "import locale; print(locale.getpreferredencoding(False))"],
        env=env, capture_output=True, text=True, encoding="ascii", errors="replace", check=True,
    )
    encoding = probe.stdout.strip().lower().replace("_", "-")
    return None if encoding in ("utf-8", "utf8") else env


def test_non_ascii_files_load_identically_under_a_non_utf8_locale(tmp_path: Path) -> None:
    env = _non_utf8_locale_env()
    if env is None:
        pytest.skip("this host always reports a UTF-8 locale encoding (macOS); "
                    "the explicit-encoding guard above still applies")
    config = _config_with_non_ascii(tmp_path)
    code = textwrap.dedent(f"""
        from server.config.loader import load_config
        from experiments.prompts import BASELINE_SYSTEM_V1
        load_config({str(config)!r})
        assert "\\u2014" in BASELINE_SYSTEM_V1, "the em dash did not survive decoding"
        print("ok")
    """)
    done = subprocess.run([sys.executable, "-X", "utf8=0", "-c", code], cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr


# ── pinned / hashed artifacts are written with LF and UTF-8 ──────────────────


def test_the_baseline_prompt_is_read_as_utf8() -> None:
    from experiments.prompts import BASELINE_SYSTEM_V1

    raw = (REPO_ROOT / "experiments" / "prompt_profiles" / "central_baseline_v1.txt").read_bytes()
    assert BASELINE_SYSTEM_V1 == raw.decode("utf-8")
    assert "\u2014" in BASELINE_SYSTEM_V1


def test_an_annotation_packet_is_byte_identical_on_every_platform(tmp_path: Path) -> None:
    from experiments import annotation_tools as at

    packet, key = tmp_path / "packet", tmp_path / "key.yaml"
    at.make_packet(packet=packet, key_path=key)
    written = (packet / "INSTRUCTIONS.md").read_bytes()
    assert b"\r\n" not in written
    assert written == at.INSTRUCTIONS.encode("utf-8")
    assert "\u2026" in at.INSTRUCTIONS  # the content really is non-ASCII


def test_the_pinned_phase04_spec_is_lf_in_the_checkout() -> None:
    from experiments import annotation_tools as at

    raw = at.P04_SPEC.read_bytes()
    assert b"\r\n" not in raw, "checked out with CRLF: .gitattributes must pin this file to LF"
    assert hashlib.sha256(raw).hexdigest() == at.P04_SPEC_SHA256
