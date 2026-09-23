"""The execution boundary's own typed contracts.

These are deliberately **not** what the runtime consumes — the runtime's
contract with the tool layer is `shared.schemas.agent.ToolInvocation` /
`ToolOutput` / `ToolHandle` / `ExecutionPlatform`, already defined and
authorized before any of these types exist, and this module reuses them
rather than inventing parallel ones (the task brief's "reuse existing
schemas wherever possible"). What lives here is the layer *underneath* that
contract: what a platform adapter (`server/tools/platforms.py`) builds from
an already-authorized `ToolInvocation` and hands to a primitive (`server/fs`,
`server/net`, `server/execution/process.py`, `server/execution/android.py`),
and what that primitive hands back.

**Why `shared/schemas/` and not `server/execution/`.** `server/fs` and
`server/net` sit *below* `server/execution` in the module-boundary layering
(`pyproject.toml`'s `[tool.importlinter]`: `execution` may import `net | fs`,
not the reverse — 16 §2's "a lower layer never imports an upper layer"), yet
both need these exact types to report a failure. The same situation is why
`shared/schemas/agent.py` holds `ToolInvocation` rather than `server/agent`:
"three modules that may not import each other all speak it." Foundation —
importable by every layer, imported by none of them — is the only place
that works.

The one invariant every type here exists to protect: **nothing in this
module can assert its own authority.** `ExecutionRequest` carries no
`authorized`, `role`, or `capability` field a caller could set to claim
permission — it carries only the already-vetted `resource_scope` a
`ToolInvocation` already had, and it is built exclusively by
`ExecutionRequest.from_invocation`, never by hand from raw agent/model
input. If a caller wants to widen what an `ExecutionRequest` may do, there
is no field to set; the only lever is a real `CapabilityGrant`, and that
lever is not reachable from any module that imports this one (`server.fs`,
`server.net`, `server.execution` do not, and by the module-boundary
contracts in `pyproject.toml` cannot, import `server.capabilities` or
`server.graph`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping
from uuid import UUID

from shared.schemas.agent import ExecutionPlatform, ToolInvocation


class ExecutionErrorCode(str, Enum):
    """Every deterministic failure this layer can produce (§11 of the task
    brief). A security failure here is always one of these — never a bare
    exception message that could leak internal detail into a `ToolOutput`."""

    SANDBOX_VIOLATION = "sandbox_violation"
    FORBIDDEN_PATH = "forbidden_path"
    EGRESS_DENIED = "egress_denied"
    DESTINATION_UNRESOLVED = "destination_unresolved"
    RESPONSE_TOO_LARGE = "response_too_large"
    PLATFORM_UNSUPPORTED = "platform_unsupported"
    DEVICE_UNAVAILABLE = "device_unavailable"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    INVALID_ARGUMENTS = "invalid_arguments"
    MISSING_CONTEXT = "missing_execution_context"
    UNAUTHORIZED_EXECUTABLE = "unauthorized_executable"
    UNKNOWN_TOOL = "unknown_tool"
    INTERNAL = "internal_error"


class ExecutionError(Exception):
    """Raised by a primitive (fs/net/process/android) and caught at the
    adapter boundary (`server/tools/platforms.py`), which turns it into a
    `ToolOutput(ok=False, error=code.value)` — it never propagates into the
    runtime as a raw exception (FAIL-006: a tool failure is an observation).
    """

    def __init__(self, code: ExecutionErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ExecutionResult:
    """What a primitive returns on success. An adapter maps this onto
    `ToolOutput(ok=True, ...)` — this type carries no `ok` field of its own
    because a primitive that fails raises `ExecutionError` instead of
    returning a falsy result (no "did it work?" field to get wrong)."""

    content: str = ""
    units: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResourceScope:
    """A typed view over `ToolInvocation.resource_scope` — the authorization
    engine's already-vetted narrowing (07 §2's `resource_scope`), never
    re-derived or widened here. `require` fails closed (`MISSING_CONTEXT`)
    rather than defaulting a missing scope key to "everything"."""

    raw: Mapping[str, str]

    def get(self, key: str) -> str | None:
        return self.raw.get(key)

    def require(self, key: str) -> str:
        value = self.raw.get(key)
        if not value:
            raise ExecutionError(
                ExecutionErrorCode.MISSING_CONTEXT,
                f"resource_scope is missing required key {key!r}",
            )
        return value

    @classmethod
    def from_invocation(cls, invocation: ToolInvocation) -> "ResourceScope":
        return cls(raw=dict(invocation.resource_scope or {}))


@dataclass(frozen=True)
class ExecutionRequest:
    """The authorized operation, in the execution layer's own vocabulary.

    Built only by `from_invocation` — every field here is copied from an
    already-authorized `ToolInvocation` (07 §8 has already run: registered,
    enabled, capability-granted, visibility-checked, tiered, confirmed where
    required). There is no other constructor path, so an `ExecutionRequest`
    cannot be fabricated from raw model output — it can only be a faithful
    narrowing of something the deterministic chain already approved.
    """

    tool_id: str
    operation: str
    user_id: UUID
    task_id: UUID
    platform: ExecutionPlatform
    arguments: Mapping[str, Any]
    resource_ref: str | None
    scope: ResourceScope
    device_id: UUID | None = None

    @classmethod
    def from_invocation(cls, invocation: ToolInvocation) -> "ExecutionRequest":
        return cls(
            tool_id=invocation.tool_id,
            operation=invocation.operation,
            user_id=invocation.user_id,
            task_id=invocation.task_id,
            platform=invocation.platform,
            arguments=dict(invocation.arguments),
            resource_ref=invocation.resource_ref,
            scope=ResourceScope.from_invocation(invocation),
            device_id=invocation.device_id,
        )


@dataclass(frozen=True)
class EgressPolicy:
    """What a network-capable tool contract declared (10 §2), resolved once
    at tool construction — never re-read from agent input, never widened
    per-call. `server/net` enforces exactly this and nothing more."""

    destinations: frozenset[str] = frozenset()
    internet: bool = False
    private_net: bool = False
    may_send_credentials: bool = False
    allowed_ports: frozenset[int] = frozenset({443, 80})
    max_response_bytes: int = 5_000_000
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 10.0
    max_redirects: int = 3


__all__ = [
    "EgressPolicy",
    "ExecutionError",
    "ExecutionErrorCode",
    "ExecutionRequest",
    "ExecutionResult",
    "ResourceScope",
]
