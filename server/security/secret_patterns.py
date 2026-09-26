"""Deterministic detection of secret-shaped text (docs/21 §4 step 3, 12 §6).

Used wherever user-derived text is about to be persisted outside the
SecretStore: the memory write gate and vault ingestion. A match *rejects*; it
never redacts and stores the rest, because a partially recognised credential
is still a credential.

`find_secret` returns the pattern's **name** only. Callers audit that name —
never the matched value — so the detector itself cannot become the leak
(MP-T5: "rejected and audited without the value").

This module imports nothing but the standard library on purpose: `server.memory`
and `server.vault` may not reach `server.secrets` by any path (pyproject
contract "Memory/vault never resolve secrets"), and detection needs no key.
"""

from __future__ import annotations

import math
import re
from collections import Counter

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key_block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----")),
    ("openai_style_key", re.compile(r"\bsk-(?:proj-|ant-|live-|test-)?[A-Za-z0-9_\-]{16,}")),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[A-Z0-9]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-.=]{20,}")),
    ("url_credentials", re.compile(r"(?i)\b[a-z][a-z0-9+\-.]*://[^\s/:@]+:[^\s/@]+@")),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:password|passwd|pwd|passphrase|secret|api[_\- ]?key|access[_\- ]?token|"
            r"auth[_\- ]?token|private[_\- ]?key|client[_\- ]?secret|pin(?:\s*code)?)\b"
            r"\s*(?:is|was|=|:)\s*\S{4,}"
        ),
    ),
    ("hypermind_secret_ref", re.compile(r"\bsecretstore:[A-Za-z0-9_\-./]+")),
)

# A run of token characters long enough, and random enough, to be a key rather
# than a word. Hex is scored separately: 40 hex characters (a git SHA, a
# fingerprint) sit well below the base64 threshold, and is not treated as a
# credential by itself.
_TOKEN_RE = re.compile(r"[A-Za-z0-9+/_\-=]{24,}")
_ENTROPY_THRESHOLD_BITS = 4.0
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def _shannon_bits(text: str) -> float:
    counts = Counter(text)
    total = len(text)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def _looks_random(token: str) -> bool:
    if _HEX_RE.match(token):
        return False
    has_digit = any(c.isdigit() for c in token)
    has_alpha = any(c.isalpha() for c in token)
    mixed_case = any(c.islower() for c in token) and any(c.isupper() for c in token)
    if not (has_digit and has_alpha and mixed_case):
        return False
    return _shannon_bits(token) >= _ENTROPY_THRESHOLD_BITS


def find_secret(text: str) -> str | None:
    """The name of the first secret pattern `text` matches, else `None`."""

    for name, pattern in _PATTERNS:
        if pattern.search(text):
            return name
    for token in _TOKEN_RE.findall(text):
        if _looks_random(token):
            return "high_entropy_token"
    return None


__all__ = ["find_secret"]
