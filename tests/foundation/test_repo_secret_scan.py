"""CFG-T3 / SS-T1 (repository half): no secret literal in any tracked file.

`test_config.py` covers the config *template*; the 15 §8 hook says "no secret
literal exists anywhere in the repo". This scans every file `git ls-files`
lists — source, tests, docs, Android, shared artifacts, binaries read as
latin-1 — with the server's own secret-pattern set (`server.security.
secret_patterns`) plus the provider formats the APK check adds.

A match passes only if it is an **unmistakable fixture**: its line carries a
`TESTONLY` / `TEST-ONLY` marker, or it is one of the two named placeholder
lines below. Anything else — a real-looking key anywhere in the tree — fails
the build, with the file and pattern named and the value never printed.

`secretstore:<name>` handles are skipped: a handle is the reference 12 §2 puts
in config *instead of* a value. The high-entropy heuristic is not applied (a
lockfile or a hash would trip it); the named formats are.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from server.security import secret_patterns

ROOT = Path(__file__).resolve().parents[2]

_SKIP = {"hypermind_secret_ref", "credential_assignment"}
_EXTRA = (
    ("groq_api_key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}")),
    ("fcm_legacy_server_key", re.compile(r"\bAAAA[A-Za-z0-9_\-]{7}:[A-Za-z0-9_\-]{140}")),
)
PATTERNS = tuple((n, p) for n, p in secret_patterns._PATTERNS if n not in _SKIP) + _EXTRA

FIXTURE_MARKER = re.compile(r"TEST-?ONLY")
# Placeholders that are not credentials but happen to have the URL shape.
PLACEHOLDER_LINES = {
    ("alembic.ini", "sqlalchemy.url = driver://user:pass@localhost/dbname"),  # TEST-ONLY placeholder
    ("tests/voice/test_voice_config_and_api.py", '"endpoint": "https://user:pw@v.test"'),  # TEST-ONLY
}


def _tracked_files() -> list[str]:
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout; the CI job runs this on one")
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True)
    return [p for p in out.stdout.decode().split("\0") if p]


def findings(files: list[str], root: Path = ROOT) -> list[str]:
    found: list[str] = []
    for rel in files:
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_bytes().decode("latin-1")
        for name, pattern in PATTERNS:
            for match in pattern.finditer(text):
                start = text.rfind("\n", 0, match.start()) + 1
                end = text.find("\n", match.end())
                line = text[start:end if end != -1 else len(text)]
                if FIXTURE_MARKER.search(line):
                    continue
                if any(rel == f and snippet in line for f, snippet in PLACEHOLDER_LINES):
                    continue
                found.append(f"{rel}:{text.count(chr(10), 0, match.start()) + 1}: {name}")
    return found


def test_no_tracked_file_contains_a_secret_literal():
    files = _tracked_files()
    assert len(files) > 100, "suspiciously few tracked files — refusing a vacuous pass"
    assert findings(files) == []


def test_the_scan_catches_a_planted_key(tmp_path):
    planted = {
        "a.py": 'KEY = "sk-proj-' + "Ab3d" * 8 + '"\n',
        "b.kt": 'val k = "AIza' + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q" + '"\n',
        "c.json": '{"private_key": "-----BEGIN PRIV' + 'ATE KEY-----\\nMIIEv..."}\n',
        "d.txt": "groq " + "gsk_" + "Q" * 30 + "\n",
        "e.py": 'X = "sk-proj-TESTONLY' + "q" * 24 + '"  # a fixture\n',
    }
    for name, text in planted.items():
        (tmp_path / name).write_text(text)
    hits = findings(sorted(planted), root=tmp_path)
    assert {h.split(": ")[1] for h in hits} == {"openai_style_key", "google_api_key", "private_key_block",
                                                 "groq_api_key"}
    assert not any(h.startswith("e.py") for h in hits)  # the marked fixture passes
    assert not any("Ab3d" in h or "gsk_" in h for h in hits)  # names the pattern, never the value
