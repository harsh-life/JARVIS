"""ANDC-T11 / REPO-T2: the built APK carries no server secret.

    python tests/tools/check_apk_secrets.py android/app/build/outputs/apk/debug/app-debug.apk

Scans every entry of the APK (dex, resources, assets, manifest) for the
key-format patterns the server's own redactor uses (private-key blocks, cloud
and provider API keys, tokens, credentialed URLs) and for the server-side
configuration names that must never ship to a phone. Binary entries are
scanned as latin-1 so byte strings embedded in dex or resources.arsc are seen.
Exits non-zero, naming the entry and pattern, on any hit.

It deliberately does not apply the high-entropy heuristic: compiled dex is
full of high-entropy byte runs, and a heuristic that always fires is one CI
learns to ignore.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import zipfile
from pathlib import Path

# The server's own pattern set, loaded from its file alone: `secret_patterns`
# is stdlib-only, and loading it without its package keeps this check runnable
# in the Android CI job with no server dependencies installed.
_SPEC = importlib.util.spec_from_file_location(
    "_secret_patterns",
    Path(__file__).resolve().parents[2] / "server" / "security" / "secret_patterns.py",
)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_PATTERNS = _MODULE._PATTERNS

# Server configuration that has no business inside a client (12 §2/§4).
_SERVER_ONLY = re.compile(
    rb"HYPERMIND_(?:KEK|MASTER|SUPERUSER|DATABASE_URL)|secret_store_keys|kek_b64|superuser_token"
)
_SKIP_PATTERNS = {"credential_assignment"}
# Provider key formats beyond the server's redactor set — Groq's `gsk_` keys
# are the one the donor app actually compiled into its APK.
_EXTRA_PATTERNS = (("groq_api_key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}")),)


def scan(apk: Path) -> list[str]:
    findings: list[str] = []
    with zipfile.ZipFile(apk) as archive:
        names = archive.namelist()
        if not any(n.endswith(".dex") for n in names):
            findings.append("no dex in the APK — refusing to report a vacuous pass")
        for name in names:
            data = archive.read(name)
            if _SERVER_ONLY.search(data):
                findings.append(f"{name}: server-only configuration name")
            text = data.decode("latin-1")
            for pattern_name, pattern in (*_PATTERNS, *_EXTRA_PATTERNS):
                if pattern_name in _SKIP_PATTERNS:
                    continue
                if pattern.search(text):
                    findings.append(f"{name}: {pattern_name}")
    return findings


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    apk = Path(argv[0])
    findings = scan(apk)
    for finding in findings:
        print(f"SECRET-SHAPED CONTENT: {finding}")
    if findings:
        return 1
    print(f"{apk.name}: no server secret found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
