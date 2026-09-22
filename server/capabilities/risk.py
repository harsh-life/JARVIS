"""The deterministic risk-tier and confirmation policy (PERM-004/005/007).

PRD §15 `[LOCKED]`:

> The tier→disposition mapping is a deterministic table, never model judgment
> (`PERM-005`). Consequential/irreversible tiers always require human
> confirmation before execution; no timeout ever auto-approves. The three-tier
> taxonomy — **never / confirm / automatic** — is exhaustive; every operation
> is exactly one of the three (`PERM-007`).

What makes this module deterministic in the sense that matters: it takes only
the operation, the resource type, and the capability's registry entry. There is
no parameter through which a model score, a confidence value, a tool's own
opinion, or free-form text could reach it, so "the model decided this was
safe" is not expressible rather than merely discouraged (TL-T10).

The two axes are combined by taking the **more restrictive** of:

1. the *resource-operation* axis — 04 §1's six operations, which is what the
   authorization engine always has; and
2. the *capability-operation* axis — the enumerated operation's own tier from
   the registry (07 §3), present when a concrete tool operation is named.

Taking the maximum is the safe composition: a low-tier resource operation
cannot launder a high-tier concrete primitive, and vice versa.

`[IMPL]`, pending owner ratification under OD-TOOL-1, exactly as in
`registry.py` — the table's *shape* and properties are locked by PRD §15; these
specific assignments are this branch's proposal for the owner to sign.
"""

from __future__ import annotations

from enum import Enum

from server.capabilities.registry import CapabilityDefinition
from shared.schemas.authorization import Operation, ResourceType
from shared.schemas.enums import RiskCategory


class Disposition(str, Enum):
    """PERM-007's exhaustive three-tier taxonomy."""

    AUTOMATIC = "automatic"
    REQUIRE_CONFIRMATION = "require_confirmation"
    NEVER = "never"


# Severity order, so "the more restrictive of two tiers" is well defined.
_SEVERITY: dict[RiskCategory, int] = {
    RiskCategory.LOW_READ: 0,
    RiskCategory.LOW_WRITE: 1,
    RiskCategory.CONSEQUENTIAL: 2,
    RiskCategory.HIGH_IRREVERSIBLE: 3,
}


# Axis 1 — the resource-operation tier (04 §1's operations).
#
# `share` is consequential per 07 §4's own example list ("share a resource").
# `delete` is consequential because it is not reversible by the actor.
# `administer` covers the graph-management acts of 04 §4 (approving a member,
# revoking one, transferring ownership, deleting a graph): each changes who can
# reach a whole graph, which is exactly what PRD §15 means by consequential.
_OPERATION_TIERS: dict[Operation, RiskCategory] = {
    Operation.READ: RiskCategory.LOW_READ,
    Operation.CREATE: RiskCategory.LOW_WRITE,
    Operation.WRITE: RiskCategory.LOW_WRITE,
    Operation.DELETE: RiskCategory.CONSEQUENTIAL,
    Operation.SHARE: RiskCategory.CONSEQUENTIAL,
    Operation.ADMINISTER: RiskCategory.CONSEQUENTIAL,
}


# Axis 1, refined per resource type where the canonical docs are specific.
#
# RAUTH-005 / 04 §5: making a resource graph-visible is the single act that can
# expose one user's content to another. It is `consequential` by the operation
# table already; it is listed here so the intent is explicit rather than
# incidental to `share`'s default.
_RESOURCE_OPERATION_TIERS: dict[tuple[ResourceType, Operation], RiskCategory] = {
    (ResourceType.MEM0FACT, Operation.SHARE): RiskCategory.CONSEQUENTIAL,
    (ResourceType.FILERESOURCE, Operation.SHARE): RiskCategory.CONSEQUENTIAL,
    (ResourceType.SCHEDULEDJOB, Operation.SHARE): RiskCategory.CONSEQUENTIAL,
    # Deleting a graph removes a shared context for everyone in it (04 §4.4).
    (ResourceType.GRAPH, Operation.DELETE): RiskCategory.HIGH_IRREVERSIBLE,
    # Granting or revoking a capability changes what the agent may attempt at
    # all; it is never an automatic act.
    (ResourceType.CAPABILITY_GRANT, Operation.CREATE): RiskCategory.CONSEQUENTIAL,
    (ResourceType.CAPABILITY_GRANT, Operation.DELETE): RiskCategory.CONSEQUENTIAL,
}


def resource_operation_tier(resource_type: ResourceType, operation: Operation) -> RiskCategory:
    """Axis 1. Total over both enums — PERM-007 requires every operation to
    have a tier, so there is no `None` return and no caller-side default."""

    specific = _RESOURCE_OPERATION_TIERS.get((resource_type, operation))
    if specific is not None:
        return specific
    return _OPERATION_TIERS[operation]


def risk_tier(
    *,
    resource_type: ResourceType,
    operation: Operation,
    capability: CapabilityDefinition | None = None,
    capability_operation: str | None = None,
) -> RiskCategory:
    """The deterministic tier for one concrete action.

    Raises `UnknownOperation` (from the registry) if `capability_operation` is
    not in the capability's enumerated mapping — an operation outside the
    mapping is not executable even with the capability held (07 §3, TL-T8), so
    it must not receive a tier and quietly proceed.
    """

    tier = resource_operation_tier(resource_type, operation)

    if capability is not None and capability_operation is not None:
        capability_tier = capability.risk_for(capability_operation)
        if _SEVERITY[capability_tier] > _SEVERITY[tier]:
            tier = capability_tier

    return tier


def disposition(tier: RiskCategory) -> Disposition:
    """PRD §15's tier→disposition mapping.

    `[LOCKED]` consequential and high_irreversible **always** require
    confirmation. `Disposition.NEVER` is never produced from a tier: a
    prohibited action is not a fourth tier that this function could return, it
    is the absence of any capability granting it (PERM-006) — so the floor is
    checked before a tier is ever computed, in
    `server/graph/authorization.py`. Returning `NEVER` from here would imply a
    prohibited action had a risk tier and could therefore be weighed, which is
    exactly the confusion PERM-006 exists to prevent.
    """

    if tier in {RiskCategory.CONSEQUENTIAL, RiskCategory.HIGH_IRREVERSIBLE}:
        return Disposition.REQUIRE_CONFIRMATION
    return Disposition.AUTOMATIC


def requires_confirmation(tier: RiskCategory) -> bool:
    return disposition(tier) is Disposition.REQUIRE_CONFIRMATION
