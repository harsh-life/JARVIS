"""The ports the agent runtime consults (05 §11, 16 §5's pattern applied here).

`server/graph/ports.py` established the pattern this file repeats one layer
up: the authorization engine declares Protocols for what it needs so it never
imports the policy half that implements them. The agent runtime needs the
same isolation, for a stronger reason — pyproject's import-linter contracts
make it *mechanical*, not just a convention:

* `server.agent` may not import `server.graph` or `server.capabilities`
  (INV-8) — so it cannot call `AuthorizationEngine.authorize()` or
  `ConfirmationService` directly.
* `server.agent` may not import `server.secrets` (REPO-T1/SECRET-002) — so it
  cannot resolve a `secret_ref`, ever, for any reason.
* `server.agent` may not import `server.storage` or `server.gateway` (this
  branch's own addition) — so it never touches a database session or the
  HTTP layer directly.
* `server.agent` may not import its siblings `server.tools`/`server.modeltools`
  /`server.models`/`server.memory` (they occupy one independent layer band) —
  so it cannot reach into a concrete tool/model/memory implementation either.

Every one of those capabilities therefore arrives here as a `Protocol`, built
from `shared.schemas` vocabulary only. `server/gateway/runtime.py` — the
composition root, which *can* see both this file's Protocols and the concrete
`server.graph`/`server.capabilities`/`server.secrets`/`server.models`/
`server.tools`/`server.modeltools`/`server.memory` implementations — is where
concrete adapters satisfying these Protocols are built and injected. None of
those adapters need to import this module either (Protocols are structural);
they only need to match the method shapes below.
"""

from __future__ import annotations

from typing import Protocol

from shared.schemas.agent_config import ToolContract
from shared.schemas.authorization import AccessRequest, AuthorizationOutcome
from shared.schemas.runtime import (
    AgentEvent,
    GenerationPolicy,
    MemoryItem,
    ModelMessage,
    ModelResult,
    ToolInvocationRequest,
    ToolResult,
)


class RuntimeAuthorizationResult:
    """What `Authorizer.authorize` returns.

    Wraps `AuthorizationOutcome` (04 §1's decision) with the raw confirmation
    token when one was minted — the engine's own `_decide` only records *that*
    confirmation is required (`confirmation_required_for`); actually minting
    a token is `server.capabilities.ConfirmationService.issue()`'s job, called
    by the concrete `Authorizer` adapter (which, unlike this module, may
    import `server.capabilities`). Kept as a plain class rather than a
    `shared.schemas` model since it is pure runtime-internal plumbing, never
    serialized over HTTP itself (the router reads `outcome` and
    `issued_confirmation_token` to build the client-facing `AgentResult`).
    """

    __slots__ = ("outcome", "issued_confirmation_token")

    def __init__(
        self, outcome: AuthorizationOutcome, *, issued_confirmation_token: str | None = None
    ) -> None:
        self.outcome = outcome
        self.issued_confirmation_token = issued_confirmation_token


class Authorizer(Protocol):
    """04's decision, reached without importing `server.graph` (see module
    docstring). The concrete adapter binds a `Principal`, an `AsyncSession`,
    and an `AuditLogger` at construction time (server/gateway/runtime.py) —
    this Protocol's signature carries none of those, so nothing in
    `server/agent` ever touches a session or a bearer token.
    """

    async def authorize(self, request: AccessRequest) -> RuntimeAuthorizationResult: ...


class ModelInvoker(Protocol):
    """06 §1's normalized `ModelProvider.invoke`. A concrete adapter
    (`server/models`) resolves its own `secret_ref` before this call is ever
    made — this Protocol carries no secret material and no `secret_ref`
    field, so there is nothing here for the runtime to mishandle even if it
    wanted to (05's "the model cannot access secrets directly" and this
    branch's "ModelProvider may NOT access raw SecretStore material" are both
    true by construction, not by discipline).
    """

    async def invoke(
        self,
        messages: list[ModelMessage],
        policy: GenerationPolicy,
        timeout: float,
    ) -> ModelResult: ...

    async def health(self) -> bool: ...


class ToolDispatcher(Protocol):
    """07 §8's `EXEC` node — dispatch only. By the time this is called, `04`
    has already returned `allow` (or a spent confirmation token turned a
    `require_confirmation` into one); this Protocol has no way to skip that,
    because it is never given anything resembling an `AccessRequest` — only
    the already-authorized `ToolInvocationRequest`. The concrete adapter
    (`server/gateway/runtime.py`, backed by `server.tools.ToolRegistry`) is
    also where `UsageEvent(kind=tool_call)` gets written, since only it can
    reach `server.storage`.
    """

    async def dispatch(self, request: ToolInvocationRequest) -> ToolResult: ...


class MemoryHydrator(Protocol):
    """11 §3's context-hydration call, already visibility-filtered by the
    time it reaches here (11 §2's `mem0_readable` predicate is the memory
    subsystem's job, not the runtime's — see `server/memory/hydrator.py`).
    The concrete adapter closes over the authenticated principal at
    construction; this Protocol never accepts a `user_id`/`graph_id`
    parameter the caller could substitute (05 §8 confused-deputy prevention:
    the runtime can only ever hydrate *its own* principal's context).
    """

    async def hydrate(self, query: str, *, limit: int) -> list[MemoryItem]: ...


class EventRecorder(Protocol):
    """This branch's observability requirement (§13), routed through a port
    for the same reason as everything else here: emitting a *persisted*
    `AuditEvent` requires `server.storage` + `server.security`, both
    unreachable from `server.agent`. The concrete adapter maps a curated
    subset of `RuntimeEventKind` onto real `AuditAction` rows.
    """

    async def record(self, event: AgentEvent) -> None: ...


class ToolCatalog(Protocol):
    """07 §1's discovery surface, filtered to what this principal's resolved
    `AgentConfiguration` actually enables (06 §3: "the agent cannot invoke a
    model-tool that isn't configured+enabled") — context assembly (05 §7)
    renders this into the model's system message. Read-only, and carries no
    execution capability of its own; invoking one still goes through
    `ToolDispatcher`, which independently re-checks authorization.
    """

    async def list_enabled(self) -> list[ToolContract]: ...


__all__ = [
    "Authorizer",
    "EventRecorder",
    "MemoryHydrator",
    "ModelInvoker",
    "RuntimeAuthorizationResult",
    "ToolCatalog",
    "ToolDispatcher",
]
