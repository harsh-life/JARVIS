"""The audit port the SecretStore emits through (12 §5).

Why a port instead of importing `server.security`: 16 §2 places `secrets` in
the *foundation* layer and `security` in *control & policy*, and the one
rule 16 §2 locks is that a lower layer never imports an upper one. So the
store declares the shape of the sink it needs, and `server.security.audit`
satisfies it structurally — no import in either direction. The wiring
happens at the gateway composition root.

`[LOCKED]` (12 §5, SECRET-004) a `SecretAuditEvent` carries the *handle*,
the requester description, and the outcome. It never carries the secret
value, and there is no field on it capable of holding one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from shared.schemas.enums import AuditResult


@dataclass(frozen=True)
class SecretAuditEvent:
    action: str  # "secret.set" | "secret.get" | "secret.delete" | "secret.rotate"
    secret_ref: str
    requester: str  # SecretRequester.describe() — never a credential
    result: AuditResult
    reason: str | None = None


@runtime_checkable
class SecretAuditSink(Protocol):
    async def record_secret_event(self, event: SecretAuditEvent) -> None: ...


class NullSecretAuditSink:
    """Used only where auditing is genuinely not applicable — unit tests of
    the crypto path, and the bootstrap step that runs before any request
    context exists.

    Every `SecretStore` method takes its sink as a required argument — there is
    no default — so an un-audited call cannot happen by omission (12 §5). Using
    this class is an explicit, greppable choice.
    """

    async def record_secret_event(self, event: SecretAuditEvent) -> None:
        return None
