"""The authorization engine's request/decision vocabulary.

Source: 04_AUTHORIZATION_GRAPH_RESOURCE.md §1 — the `AccessRequest` shape and
its two enums (`operation`, `resource_type`).

Why these live in `shared/schemas/` rather than inside `server/graph`:
`server/graph` (the engine) and `server/capabilities` (the policy it consults)
are deliberately mutually independent modules (16 §5 — keeping the half that
*decides* a capability check separate from the half that can *grant* one), so
they need a vocabulary neither one owns. Later branches — `05`'s runtime,
`09`'s filesystem, `11`'s memory — construct `AccessRequest`s too, and 16 §1
makes `shared/schemas/` the layer everything may import.

**Not a `01` §1.2 registry extension.** These enums are engine vocabulary, not
entity field values: nothing persists them as a column. `01` §1.2's locked
registry is untouched, and adding to it would need the owner's approval
(`00` §26).

**Runtime-branch addition (`AccessRequest`/`AuthorizationOutcome`).** These two
types were defined inline in `server/graph/authorization.py` by security-core.
They are moved here, verbatim in shape and unchanged in meaning, because the
agent runtime (`05`) must be able to construct an `AccessRequest` and read an
`AuthorizationOutcome` without importing `server.graph` at all — that import is
mechanically forbidden (pyproject's "Agent cannot import the capability/authz
engine", INV-8), by design, so the model can never reach the engine's
internals. This is the same reasoning `Principal` below already documents for
itself ("the producer may import the contract, but the consumers must not have
to import the producer"), now applied to the engine's own request/decision
shape. No behaviour, validation, or decision logic moves — `server/graph/
authorization.py` still owns `readable()` and the five-dimension algorithm; it
now imports these two shapes from here instead of defining them, and
re-exports them so every existing `from server.graph.authorization import
AccessRequest` keeps working. `AuthorizationOutcome.resource` is typed loosely
(`Any`, not `server.graph.ports.ResourceDescriptor`) for the same reason: nothing
outside `server/graph` reads that field (grep-verified before this move), so
this vocabulary module has no need to import the type that would otherwise
recreate the exact coupling this move exists to remove.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping
from uuid import UUID

from pydantic import ConfigDict

from shared.schemas.common import ORMBase
from shared.schemas.enums import PermissionDecisionValue, RiskCategory


class Principal(ORMBase):
    """The authenticated principal — 03 §8's handoff contract, verbatim.

    `[LOCKED]` (03 §8) every field here is trustworthy *identity*: `03`
    validated it against a credential the server itself issued, so none of it
    came from a request body (PHONE-003). `04` re-checks *authorization*
    (membership, visibility, capability) itself — establishing identity is not
    granting access.

    `active_graph_id` is explicitly **not** trusted as a grant. 04 §9 requires
    it to be re-checked live against membership on every request, because the
    session row is a cached convenience and the user may have left the graph
    since it was written.

    This lives in `shared/schemas/` rather than `server/auth/` because
    `server/graph` and `server/capabilities` consume it while sitting *below*
    `server/auth` in the dependency order (16 §2) — the producer may import
    the contract, but the consumers must not have to import the producer.
    """

    model_config = ConfigDict(from_attributes=True, extra="forbid", frozen=True)

    user_id: UUID
    device_id: UUID
    session_id: UUID
    active_graph_id: UUID | None = None


class Operation(str, Enum):
    """04 §1 — `operation: enum(read | write | create | delete | share | administer)`."""

    READ = "read"
    WRITE = "write"
    CREATE = "create"
    DELETE = "delete"
    SHARE = "share"
    ADMINISTER = "administer"


class ResourceType(str, Enum):
    """04 §1 — `resource_type: enum(mem0fact | fileresource | scheduledjob |
    graph | membership | agentconfig | capability_grant)`.

    `SECRET_REFERENCE` is added beyond that list because 04 §5 requires the
    engine to be *able* to recognise and refuse a `share` on a secret-class
    resource (`AZ-T5`, GRAPH-009) — a rule the engine cannot enforce if a
    secret reference is not nameable as a resource type. It is never a
    shareable or graph-scoped resource; naming it exists only so the refusal
    is expressible.
    """

    MEM0FACT = "mem0fact"
    FILERESOURCE = "fileresource"
    SCHEDULEDJOB = "scheduledjob"
    GRAPH = "graph"
    MEMBERSHIP = "membership"
    AGENTCONFIG = "agentconfig"
    CAPABILITY_GRANT = "capability_grant"
    SECRET_REFERENCE = "secret_reference"

    # Runtime-branch addition, same precedent as `SECRET_REFERENCE` above: a
    # tool/model-tool invocation (05 §1, 07 §8) has no backing stored row —
    # there is nothing for a `ResourceLoader` to load — so it is always
    # expressed as `operation=CREATE` (which the engine's `_decide` skips
    # resource-loading for entirely) with this resource_type carried only for
    # audit/classification. `04`'s D1-D4 dimensions are therefore structurally
    # trivial for it (no resource, so nothing to own or hide); D5's capability
    # check plus the absolute-floor gate are what actually authorize it — the
    # same "engine vocabulary, not an `01` §1.2 registry extension" carve-out
    # already claimed for `SECRET_REFERENCE`.
    TOOL_INVOCATION = "tool_invocation"


class DenialSurface(str, Enum):
    """How a denial is surfaced to a client (04 §7).

    `[LOCKED]` (04 §7) "a denial that would reveal the existence or visibility
    of a resource the caller can't see returns 404; a denial about an
    operation on a resource the caller can already see returns 403." The
    engine records which of the two applies so the HTTP layer cannot get it
    wrong by guessing — that guess is the anti-enumeration oracle (SEC-Q/R).
    """

    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    PROHIBITED = "prohibited"


@dataclass(frozen=True)
class CapabilityCheckContext:
    """Everything the D5 capability check is allowed to consider (04 §2, 07 §2).

    Deliberately narrow, and here in `shared/` so the engine that *asks* the
    question and the grant service that *answers* it share one definition
    without importing each other (16 §5).

    Note what has no field: a model's opinion, a confidence score, a tool's own
    assertion about its permissions. "The model judged this safe" is not
    expressible through this type rather than merely discouraged (PERM-005,
    TL-T10).
    """

    principal: Principal
    graph_id: UUID | None = None
    task_id: str | None = None
    # The concrete narrowing the operation claims to stay inside — e.g.
    # {"package_name": "com.example"} or {"sandbox_root": "/…"} (07 §2).
    resource_scope: Mapping[str, str] | None = None


@dataclass(frozen=True)
class ActionBinding:
    """The exact action one confirmation token authorizes (PERM-004, 05 §4).

    Lives here for the same reason `Principal` does: the authorization engine
    (`server/graph`) builds one to verify a confirmation, and the confirmation
    service (`server/capabilities`) builds one to issue it, and those two
    modules are deliberately independent (16 §5).

    `arguments` is hashed, never stored: the hash detects any substitution,
    while keeping tool inputs — which carry user content — out of a security
    table.
    """

    principal_user_id: UUID
    task_id: str
    operation: Operation
    resource_type: ResourceType
    session_id: UUID | None = None
    capability: str | None = None
    resource_ref: str | None = None
    arguments: Mapping[str, Any] | None = None

    def arguments_hash(self) -> str:
        """A canonical, order-independent digest of the call's arguments.

        `sort_keys` and fixed separators make the encoding canonical, so the
        same logical arguments always hash identically regardless of mapping
        order — otherwise a legitimate confirmation would fail to validate at
        random. `default=str` keeps non-JSON scalars (UUID, datetime) hashable
        rather than raising during a security check.
        """

        payload = json.dumps(
            dict(self.arguments) if self.arguments else {},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── the engine's request/decision shape (moved from server/graph/authorization.py) ──


@dataclass(frozen=True)
class AccessRequest:
    """04 §1's `AccessRequest`, plus the fields the confirmation binding needs.

    `graph_id` is the *graph context of the operation* — 04 §0's "a `graph_id`
    in a request body is a claim to check, never a grant". The engine checks it
    (D1) and never treats it as permission.
    """

    principal: Principal
    operation: Operation
    resource_type: ResourceType
    resource_ref: str | None = None
    graph_id: UUID | None = None
    required_capability: str | None = None
    # The concrete enumerated operation within the capability (07 §3). Supplied
    # by tool dispatch; absent for plain resource operations.
    capability_operation: str | None = None
    # Confirmation binding inputs (PERM-004). `task_id` identifies the agent
    # task a consequential action belongs to.
    task_id: str | None = None
    arguments: Mapping[str, Any] | None = None
    confirmation_token: str | None = None
    # The narrowing the operation claims to stay inside, checked against the
    # grant's `resource_scope` (07 §2).
    resource_scope: Mapping[str, str] | None = None


@dataclass(frozen=True)
class AuthorizationOutcome:
    """04 §1's `PermissionDecision`, as returned to the caller.

    `surface` is carried explicitly so the HTTP layer never has to guess
    whether a denial is a 403 or a 404. That guess is precisely the
    anti-enumeration oracle 04 §7 exists to remove (SEC-Q/R).
    """

    decision: PermissionDecisionValue
    risk_category: RiskCategory
    reason: str
    surface: DenialSurface | None = None
    floor_category: str | None = None
    confirmation_required_for: ActionBinding | None = None
    # Untyped on purpose — see module docstring: only server/graph reads this
    # field, and it always assigns a `server.graph.ports.ResourceDescriptor`.
    resource: Any | None = field(default=None, repr=False)

    @property
    def allowed(self) -> bool:
        return self.decision is PermissionDecisionValue.ALLOW

    @property
    def needs_confirmation(self) -> bool:
        return self.decision is PermissionDecisionValue.REQUIRE_CONFIRMATION
