"""Who is asking for a secret — the vocabulary the SecretStore mediates on.

Source: 12_SECRETSTORE.md §2 (access mediation) and §4 (SUPER-001).

This module is deliberately the *bottom* of the secrets dependency
direction: the store defines what kinds of requester exist, and
`server/security/superuser.py` decides who qualifies as one. The reverse
would mean the store trusted the caller it is supposed to be mediating
(and is forbidden mechanically — see pyproject's "SecretStore never imports
the identity layer" contract).

`[LOCKED]` (12 §2, SECRET-002, INV-6) `RequesterKind.AGENT` exists here for
exactly one reason: so that `SecretStore.get` can name it and refuse it
unconditionally. There is no code path by which an agent requester resolves
a value. The structural guarantee (P2, "absence over restriction") is the
import-linter contract that stops `server.agent` importing this package at
all; this enum is the defense-in-depth backstop behind it.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from enum import Enum
from uuid import UUID


class RequesterKind(str, Enum):
    AGENT = "agent"
    TOOL = "tool"
    USER = "user"
    SERVER = "server"
    SUPERUSER = "superuser"


@dataclass(frozen=True)
class SuperuserGrant:
    """Proof that the out-of-band superuser credential was verified.

    `[LOCKED]` (12 §4, SUPER-001) superuser authority is a **separate
    principal** from any Hypermind user. This type has no public
    constructor path from a `User`, a `Session`, or an OIDC login — a human
    authenticating through Google can never become one (§15 of the
    security-core scope). It is minted only by
    `server.security.superuser.authenticate_superuser()`, which checks a
    credential the application never issues and never stores.

    Honest bound (12 §4): this is a process-local object. Code already
    executing arbitrary Python inside the application process could
    construct one. That is not a gap this type can close — it is OD-A1
    territory (`14` §4), measured rather than claimed (see
    docs/OD_A1_BR_T2.md).
    """

    token_fingerprint: str
    _verified: bool = field(default=False, repr=False)

    def is_valid(self) -> bool:
        return self._verified

    @classmethod
    def _issue(cls, token_fingerprint: str) -> "SuperuserGrant":
        """Internal mint point. Called only by server.security.superuser."""

        return cls(token_fingerprint=token_fingerprint, _verified=True)


@dataclass(frozen=True)
class SecretRequester:
    """A resolution request's authenticated origin (12 §2).

    Construct through the classmethods, never by hand — each one encodes the
    scoping facts that `SecretStore.get` mediates on.
    """

    kind: RequesterKind
    user_id: UUID | None = None
    graph_id: UUID | None = None
    tool_id: str | None = None
    # 12 §2 / SS-T10: a tool receives only the secret its own
    # ToolConfiguration declares — never another scope's, never one it did
    # not declare a need for.
    declared_secret_refs: frozenset[str] = frozenset()
    superuser_grant: SuperuserGrant | None = None

    @classmethod
    def agent(cls) -> "SecretRequester":
        """The agent runtime. Always denied by `get` (SECRET-002/INV-6)."""

        return cls(kind=RequesterKind.AGENT)

    @classmethod
    def user(cls, user_id: UUID, *, graph_id: UUID | None = None) -> "SecretRequester":
        return cls(kind=RequesterKind.USER, user_id=user_id, graph_id=graph_id)

    @classmethod
    def tool(
        cls,
        tool_id: str,
        *,
        declared_secret_refs: frozenset[str] | set[str],
        user_id: UUID | None = None,
        graph_id: UUID | None = None,
    ) -> "SecretRequester":
        return cls(
            kind=RequesterKind.TOOL,
            tool_id=tool_id,
            user_id=user_id,
            graph_id=graph_id,
            declared_secret_refs=frozenset(declared_secret_refs),
        )

    @classmethod
    def server(cls, *, graph_id: UUID | None = None) -> "SecretRequester":
        """Deterministic server-owned infrastructure resolving a handle at the
        boundary (12 §1: "only ever called by the deterministic resolution
        boundary")."""

        return cls(kind=RequesterKind.SERVER, graph_id=graph_id)

    @classmethod
    def superuser(cls, grant: SuperuserGrant) -> "SecretRequester":
        if not grant.is_valid():
            raise ValueError("superuser requester requires a verified SuperuserGrant")
        return cls(kind=RequesterKind.SUPERUSER, superuser_grant=grant)

    @property
    def is_superuser(self) -> bool:
        return (
            self.kind is RequesterKind.SUPERUSER
            and self.superuser_grant is not None
            and self.superuser_grant.is_valid()
        )

    def describe(self) -> str:
        """A log/audit-safe description. Carries no secret material and no
        credential fingerprint (SECRET-004)."""

        if self.kind is RequesterKind.TOOL:
            return f"tool:{self.tool_id}"
        if self.kind is RequesterKind.USER:
            return f"user:{self.user_id}"
        return self.kind.value


def fingerprint(value: str) -> str:
    """A short, non-reversible label for a credential, safe to audit.

    Used only so an operator can tell *which* superuser credential was
    presented across events. Never the value, never enough of it to
    reconstruct (SECRET-004).
    """

    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
