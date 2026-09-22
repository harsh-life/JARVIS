"""Per-tool destination allow-listing (10 §2's `ToolContract.network.
destinations`).

A hostname check is *scoping*, not the security boundary — `policy.py`'s
resolved-IP classification is what actually holds against SSRF/rebinding.
This module only decides "did the tool declare this host at all", the same
way a `ToolContract` declares any other boundary the enforcement layer then
holds it to (07 §5).
"""

from __future__ import annotations

from typing import Iterable


def hostname_allowed(host: str, destinations: Iterable[str]) -> bool:
    """`destinations` entries are either an exact host (`api.example.com`)
    or a subdomain wildcard written as a leading dot (`.example.com`, which
    matches `foo.example.com` but deliberately not `example.com` itself —
    an operator who wants both lists both, rather than a bare domain
    silently matching every subdomain it does not administer)."""

    normalized_host = host.strip().lower().rstrip(".")
    if not normalized_host:
        return False
    for pattern in destinations:
        normalized_pattern = pattern.strip().lower().rstrip(".")
        if not normalized_pattern:
            continue
        if normalized_pattern.startswith("."):
            if normalized_host.endswith(normalized_pattern):
                return True
        elif normalized_host == normalized_pattern:
            return True
    return False


__all__ = ["hostname_allowed"]
