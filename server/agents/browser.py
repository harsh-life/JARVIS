"""A Browser Use run's pure policy — Phase 6 slice 6D (OD-AF-6, OD-AF-15).

`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]` (docs/29). Pure: no store, no
engine, no container. What it decides, from JARVIS facts only:

* **the hosts** — `browser.session`/`browse` (OD-AF-15, `low_write`) is
  scoped to an explicit list of exact host names. The compiler takes them
  from the draft's URL sources (`hosts_from_urls`; plain `https` host names
  only — no IP literal, no userinfo, no single-label name) and writes them
  into the envelope entry's scope; a run reads them back from the stored,
  hash-verified spec (`browse_hosts`). The runtime never supplies them, and
  Browser Use's own `allowed_domains` is only a copy of them, advisory.
* **the task file** the container reads (`task_document`) — JARVIS's own
  assembled input and limits;
* **the result** the container writes (`read_result`) — untrusted data:
  bounded in bytes, strictly shaped, no extra field; anything else is a
  failed run (`result_unreadable`), never a guess. What survives becomes an
  inbox item through the ordinary path (scrubbed, bounded), and changes no
  authority.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Iterable, Sequence
from urllib.parse import urlsplit

from shared.schemas.agent_factory import EnvelopeEntry

BROWSER_CAPABILITY = "browser.session"
BROWSE = "browse"
SCOPE_KEY = "hosts"
MAX_HOSTS = 32
MAX_RESULT_BYTES = 64 * 1024
MAX_FINAL_CHARS = 16_000
_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
_HOST = re.compile(rf"^(?:{_LABEL}\.)+{_LABEL}$")


def _exact_host(host: str) -> str:
    name = host.strip().lower().removesuffix(".")
    if not _HOST.fullmatch(name):
        raise ValueError(f"not an exact host name: {host!r}")
    try:
        ipaddress.ip_address(name)
    except ValueError:
        if re.fullmatch(r"0x[0-9a-f]*|[0-9]+", name.rsplit(".", 1)[-1]):
            raise ValueError(f"an IP address is not a host name: {host!r}") from None
        return name
    raise ValueError(f"an IP address is not a host name: {host!r}")


def hosts_from_urls(urls: Iterable[str]) -> tuple[str, ...]:
    """The exact hosts of `https` URLs, sorted and unique."""

    hosts = set()
    for url in urls:
        parts = urlsplit(url.strip())
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password \
                or parts.port not in (None, 443):
            raise ValueError(f"not a plain https URL: {url!r}")
        hosts.add(_exact_host(parts.hostname))
    if not hosts or len(hosts) > MAX_HOSTS:
        raise ValueError("a browser agent needs between 1 and 32 hosts")
    return tuple(sorted(hosts))


def hosts_scope(hosts: Sequence[str]) -> dict[str, str]:
    names = sorted({_exact_host(h) for h in hosts})
    if not names or len(names) > MAX_HOSTS:
        raise ValueError("a browser agent needs between 1 and 32 hosts")
    return {SCOPE_KEY: ",".join(names)}


def browse_hosts(envelope: Iterable[EnvelopeEntry]) -> tuple[str, ...]:
    """The hosts the spec's `browser.session` entry names (none: no entry)."""

    for entry in envelope:
        if entry.capability == BROWSER_CAPABILITY and BROWSE in entry.operations:
            raw = entry.scope.get(SCOPE_KEY, "")
            return tuple(sorted({_exact_host(h) for h in raw.split(",") if h}))
    return ()


def task_document(task: str, hosts: Sequence[str], *, max_steps: int) -> str:
    return json.dumps({"task": task, "hosts": list(hosts), "max_steps": int(max_steps)})


@dataclass(frozen=True)
class BrowserResult:
    status: str                 # completed | failed
    final: str | None
    steps: int
    error: str | None


_UNREADABLE = BrowserResult(status="failed", final=None, steps=0, error="result_unreadable")


def read_result(raw: bytes) -> BrowserResult:
    if not raw or len(raw) > MAX_RESULT_BYTES:
        return _UNREADABLE
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _UNREADABLE
    if not isinstance(data, dict) or set(data) != {"status", "final", "steps", "error"}:
        return _UNREADABLE
    status, final, steps, error = data["status"], data["final"], data["steps"], data["error"]
    if status not in ("completed", "failed") or not isinstance(steps, int) or isinstance(steps, bool) \
            or not (final is None or isinstance(final, str)) or not (error is None or isinstance(error, str)):
        return _UNREADABLE
    if status == "completed" and final is None:
        return _UNREADABLE
    return BrowserResult(status=status, final=final[:MAX_FINAL_CHARS] if final is not None else None,
                         steps=max(0, steps), error=re.sub(r"[^a-z0-9_]", "", error.lower())[:40] if error else None)


__all__ = [
    "BROWSE",
    "BROWSER_CAPABILITY",
    "MAX_RESULT_BYTES",
    "BrowserResult",
    "browse_hosts",
    "hosts_from_urls",
    "hosts_scope",
    "read_result",
    "task_document",
]
