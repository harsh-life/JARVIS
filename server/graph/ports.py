"""The ports the authorization engine consults (04 §2, 16 §5).

16 §5 treats `graph` and `capabilities` as the two halves of one authorization
engine, and they are kept **mutually independent** modules on purpose: the half
that evaluates "is this capability granted?" must not be able to grant one, and
the half that issues confirmations must not be able to decide it already has
one. Neither imports the other; the engine declares what it needs here, and the
gateway composition root supplies `server/capabilities`' implementations.

These are `Protocol`s, so satisfying them requires no inheritance and therefore
no import in the other direction either. The vocabulary they speak
(`Principal`, `Operation`, `ResourceType`, `ActionBinding`, `RiskCategory`)
lives in `shared/schemas/`, which 16 §1 makes importable by both.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from shared.schemas.authorization import (
    ActionBinding,
    CapabilityCheckContext,
    Operation,
    ResourceType,
)
from shared.schemas.enums import RiskCategory, Visibility


@dataclass(frozen=True)
class ResourceDescriptor:
    """The authorization-relevant facts about one resource — and only those.

    Every visibility-bearing entity in Track B carries `01` §1.1's visibility
    triplet (`visibility`, `owner_user_id`, `source_user_id`) plus an optional
    `graph_id` scoping field. The engine needs exactly those four things, so a
    loader projects a row down to this and nothing else: the engine cannot
    accidentally base a decision on resource *content*, because it never sees
    any.

    `[LOCKED]` (04 §9) a resource with `graph_id` set but `visibility` missing
    is treated as `private`. A loader must therefore never leave `visibility`
    unset — the field is non-optional here so the safe default has to be
    applied at projection time rather than assumed downstream.
    """

    resource_type: ResourceType
    resource_ref: str
    owner_user_id: UUID
    visibility: Visibility
    graph_id: UUID | None = None
    source_user_id: UUID | None = None


class ResourceLoader(Protocol):
    """Resolves a `resource_ref` to its authorization facts.

    Returning `None` means "not loadable", which the engine turns into a
    `404`-surfaced denial. That is the correct answer both for a resource that
    does not exist and for a resource type no branch has implemented yet —
    fail-closed either way (04 §9).
    """

    async def load(
        self,
        session: AsyncSession,
        resource_type: ResourceType,
        resource_ref: str,
    ) -> ResourceDescriptor | None: ...


class CapabilityChecker(Protocol):
    """D5 (04 §2). Satisfied by `server.capabilities.CapabilityGrantService`.

    Returns a bool and takes no resource, so it is structurally incapable of
    standing in for the visibility check (AZ-T7/TL-T3: holding `file.read`
    never reads another user's private file).
    """

    async def has_capability(
        self,
        session: AsyncSession,
        *,
        capability: str,
        context: Any,
        capability_operation: str | None = None,
    ) -> bool: ...


class RiskPolicy(Protocol):
    """PERM-004/005. Satisfied by `server.capabilities.policy`.

    Note what is absent from the signature: no model output, no confidence
    score, no free-form text. The tier is a function of the operation, the
    resource type, the capability's registry entry and — for device
    operations — the app it acts in (`resource_scope`, the sensitive-app
    classification, docs/CAPABILITY_MATRIX.md §5.1). Nothing else can reach it
    (TL-T10).
    """

    def risk_tier(
        self,
        *,
        resource_type: ResourceType,
        operation: Operation,
        capability_name: str | None,
        capability_operation: str | None,
        resource_scope: Mapping[str, str] | None = None,
    ) -> RiskCategory: ...

    def scope_denial(
        self,
        *,
        capability_name: str | None,
        capability_operation: str | None,
        resource_scope: Mapping[str, str] | None,
    ) -> str | None: ...

    def requires_confirmation(self, tier: RiskCategory) -> bool: ...


class FloorPolicy(Protocol):
    """PERM-006's backstop gate (04 §2). Satisfied by `server.capabilities.floor`.

    Returns the prohibition's category name, or `None`. Typed as `str` rather
    than the enum so the engine needs no import from `server.capabilities`;
    `AbsoluteFloorCategory` is a `str` enum and satisfies it directly.
    """

    def floor_category_for_request(
        self,
        *,
        capability: str | None,
        operation: Operation,
        resource_type: ResourceType,
    ) -> str | None: ...


class ConfirmationVerifier(Protocol):
    """PERM-004's `already_confirmed(request)` (04 §3).

    Satisfied by `server.capabilities.ConfirmationService`. `consume` spends the
    token as it validates: a confirmation authorizes one attempt, so validating
    and spending cannot be separate steps that a retry could repeat.
    """

    async def consume(
        self, session: AsyncSession, *, token: str, binding: ActionBinding
    ) -> bool: ...


class MembershipReader(Protocol):
    """D1 (04 §2). Satisfied by `server.graph.repository.GraphRepository`.

    A port rather than a direct call so the OD-A1 experiment and the
    fail-closed tests can substitute a reader that raises, and assert the
    engine denies rather than allows (AZ-T9).
    """

    async def active_role(
        self, session: AsyncSession, *, graph_id: UUID, user_id: UUID
    ) -> Any | None: ...


# Re-exported so the engine and its tests have one obvious import site for the
# context type; it is defined in shared/schemas because both halves of the
# engine construct it (16 §5).
__all__ = [
    "CapabilityChecker",
    "CapabilityCheckContext",
    "ConfirmationVerifier",
    "FloorPolicy",
    "MembershipReader",
    "ResourceDescriptor",
    "ResourceLoader",
    "RiskPolicy",
]
