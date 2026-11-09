"""Run traces (docs/31 §4): what ran, on which configuration, at what cost.

A run directory holds `run.json` (metadata, including the snapshot's
configuration hash), `calls.jsonl` (one record per model call), `outputs.jsonl`
(one per task: status, output, score, content hashes of input and output) and
`summary.json`. Results directories are not committed.

Two guards keep secrets out: `SecretScanner.check_prompt` refuses to send a
prompt that contains a secret-shaped string (wired into every metered call),
and everything written to disk passes `SecretScanner.redact` first. No raw
screenshots, OCR, memory or tool observations exist in the harness to retain.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Iterable

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"secretstore:[A-Za-z0-9:_-]+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{16,}"),
]


class SecretInPrompt(Exception):
    """A prompt contained a secret-shaped string; it was not sent."""


class SecretScanner:
    def __init__(self, known_values: Iterable[str] = ()) -> None:
        self.known = [v for v in known_values if v and len(v) >= 8]
        self.redactions = 0
        self.blocked_prompts = 0

    def _hits(self, text: str) -> bool:
        return any(p.search(text) for p in SECRET_PATTERNS) or any(v in text for v in self.known)

    def check_prompt(self, text: str) -> None:
        if self._hits(text):
            self.blocked_prompts += 1
            raise SecretInPrompt("a prompt contained a secret-shaped string and was not sent")

    def redact(self, text: str) -> str:
        out = text
        for p in SECRET_PATTERNS:
            out, n = p.subn("[REDACTED]", out)
            self.redactions += n
        for v in self.known:
            if v in out:
                self.redactions += out.count(v)
                out = out.replace(v, "[REDACTED]")
        return out


def content_ref(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


class TraceWriter:
    def __init__(self, directory: Path, scanner: SecretScanner) -> None:
        self.dir = directory
        self.scanner = scanner
        self.dir.mkdir(parents=True, exist_ok=False)

    def _dump(self, value) -> str:
        return self.scanner.redact(json.dumps(value, ensure_ascii=False, default=str))

    def write_json(self, name: str, value) -> None:
        (self.dir / name).write_text(self.scanner.redact(json.dumps(value, ensure_ascii=False, indent=2, default=str)))

    def append(self, name: str, value) -> None:
        with (self.dir / name).open("a") as fh:
            fh.write(self._dump(value) + "\n")


__all__ = ["SecretInPrompt", "SecretScanner", "TraceWriter", "content_ref"]
