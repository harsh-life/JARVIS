"""Server-side redaction for the operator console (DASH-005, DASH-006, 28 §4).

Applied before a response leaves the route, so the console never receives what
it is supposed to hide.

* **User content** — a task's response, an evaluator's note about it, a
  candidate's proposed text — is withheld by default and replaced by its
  length (`redact_user_text`). Seeing it is a separate, privileged, audited
  request (DASH-006).
* **Secrets** — a configured credential is shown as its *handle* and whether it
  resolves, never a value (`config_view`). Anything else in the configuration
  that looks like a credential (e.g. a password inside a database URL) is
  masked by the repository's secret patterns (`scrub`).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from server.security.secret_patterns import redact_secrets


def redact_user_text(text: str | None) -> dict | None:
    if text is None:
        return None
    return {"redacted": True, "chars": len(text)}


def scrub(text: str) -> str:
    """Identifiers and names pass unchanged; anything secret-shaped is masked."""

    return redact_secrets(text)[0]


def _is_reference(key: str) -> bool:
    return key.endswith("_ref") or key == "kek_source"


async def config_view(value: Any, resolves: Callable[[str], Awaitable[bool | None]], *, key: str = "") -> Any:
    """The effective configuration, secret-free: every `*_ref` / `kek_source`
    becomes `{"handle": …, "resolves": …}`, and every other string is scrubbed."""

    if isinstance(value, dict):
        return {k: await config_view(v, resolves, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [await config_view(v, resolves, key=key) for v in value]
    if isinstance(value, str):
        if _is_reference(key):
            # A handle is metadata the console may show (DASH-005); config
            # validation already refuses anything but `env:` / `secretstore:`.
            return {"handle": value, "resolves": await resolves(value)}
        return scrub(value)
    return value


__all__ = ["config_view", "redact_user_text", "scrub"]
