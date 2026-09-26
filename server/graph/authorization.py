"""The five-dimension resource-authorization engine (04_AUTHORIZATION_GRAPH_RESOURCE.md).

04 §0, the one sentence this module exists to enforce:

> **Being a member of a graph does not grant access to every resource in it.**

This is the single most important correctness surface in Track B, and it is the
**only** place in the codebase that decides resource access. Every other
subsystem — `05`'s runtime, `07`'s tool dispatch, `09`'s filesystem, `11`'s
memory — asks this engine rather than re-deriving the answer, so there is one
predicate to get right and one place to test (04 §1: "No resource operation
happens without passing through this engine — there is no side door").

The four properties 04 §3 locks, and where each one lives here:

* **Fail-closed** — `authorize()` wraps the whole decision path; any exception,
  from a membership lookup to a resource load, becomes a denial. There is no
  `except` clause that continues.
* **Deterministic** — no model, score, or free-form text reaches this code. The
  inputs are a validated `Principal` (03 §8) and an `AccessRequest` of enums
  and ids. An LLM may *propose* the request; it never influences the decision
  (INV-1/INV-2).
* **Total** — every path returns an `AuthorizationOutcome`. There is no implicit
  allow, and the final `allow` is reached only by falling through every gate.
* **Audited** — every decision writes both a `PermissionDecision` and an
  `AuditEvent` before returning (AZ-T12).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from server.graph.predicate import readable  # re-exported: the one RAUTH-004 predicate
from server.graph.ports import (
    CapabilityChecker,
    ConfirmationVerifier,
    FloorPolicy,
    MembershipReader,
    ResourceDescriptor,
    ResourceLoader,
    RiskPolicy,
)
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from shared.schemas.authorization import (
    ActionBinding,
    CapabilityCheckContext,
    DenialSurface,
    Operation,
    Principal,
    ResourceType,
)
from shared.schemas.enums import (
    AuditActor,
    AuditResult,
    MembershipRole,
    PermissionDecisionValue,
    RiskCategory,
)

logger = logging.getLogger("hypermind.graph.authorization")


# ── requests and outcomes (04 §1) ───────────────────────────────────────────


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
    graph_id: uuid.UUID | None = None
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
    resource: ResourceDescriptor | None = field(default=None, repr=False)

    @property
    def allowed(self) -> bool:
        return self.decision is PermissionDecisionValue.ALLOW

    @property
    def needs_confirmation(self) -> bool:
        return self.decision is PermissionDecisionValue.REQUIRE_CONFIRMATION


# Operations that only the resource's owner may perform (04 §2 D3).
#
# 04 §3's algorithm lists `{delete, share, change_visibility}`; `WRITE` is
# included here because 04 §2 D2 grants a `member` write on "**their own**
# resources" and D3 states that other members of a shared resource's graph "can
# *read* it, not *re-govern* it". Both readings converge on "a member cannot
# write another member's resource"; enforcing it at D3 is a placement choice,
# not a new rule. `share` covers 04 §5's `change_visibility` — they are the same
# operation in `04` §1's enum.
_OWNER_ONLY_OPERATIONS = frozenset(
    {Operation.DELETE, Operation.SHARE, Operation.WRITE}
)

# Operations that require the graph-owner role rather than mere membership
# (04 §2 D2: "A `member` cannot approve members, cannot delete the graph,
# cannot administer another member").
_OWNER_ROLE_OPERATIONS = frozenset({Operation.ADMINISTER})


class AuthorizationEngine:
    """The one authoritative authorization service.

    Constructed once and shared; all per-request state arrives as arguments, so
    there is no instance field a concurrent request could observe.
    """

    def __init__(
        self,
        *,
        memberships: MembershipReader,
        resources: ResourceLoader,
        capabilities: CapabilityChecker,
        risk: RiskPolicy,
        floor: FloorPolicy,
        confirmations: ConfirmationVerifier,
    ) -> None:
        self._memberships = memberships
        self._resources = resources
        self._capabilities = capabilities
        self._risk = risk
        self._floor = floor
        self._confirmations = confirmations

    def risk_tier_for(
        self,
        *,
        resource_type: ResourceType,
        operation: Operation,
        capability: str | None,
        capability_operation: str | None,
        resource_scope: Mapping[str, str] | None = None,
    ) -> RiskCategory:
        """The tier this engine would compute for an action — the same policy
        object `_decide` uses, exposed read-only so the supervisor's mode
        ceiling (18 §3) reads the one tier table rather than a copy of it. It
        decides nothing: it grants, denies and confirms nothing."""

        return self._risk.risk_tier(
            resource_type=resource_type,
            operation=operation,
            capability_name=capability,
            capability_operation=capability_operation,
            resource_scope=resource_scope,
        )

    async def authorize(
        self, session: AsyncSession, request: AccessRequest, *, audit: AuditLogger
    ) -> AuthorizationOutcome:
        """Run 04 §3's canonical decision algorithm.

        The fail-closed wrapper is the outermost layer on purpose: a bug or an
        infrastructure failure anywhere inside becomes `deny`, never `allow`
        (FAIL-CORE-003, AZ-T9). The exception is logged for operators and
        deliberately not surfaced to the caller — an error message that
        distinguished "membership lookup failed" from "you are not a member"
        would be an enumeration oracle.
        """

        try:
            outcome = await self._decide(session, request)
        except Exception:  # noqa: BLE001 — fail-closed is the whole point
            logger.exception(
                "authorization path failed; denying (fail-closed, FAIL-CORE-003)"
            )
            outcome = AuthorizationOutcome(
                decision=PermissionDecisionValue.DENY,
                risk_category=RiskCategory.LOW_READ,
                reason="authorization_unavailable",
                # A read denial stays 404 even on an internal failure, so an
                # induced error cannot become a way to distinguish an existing
                # private resource from an absent one.
                surface=(
                    DenialSurface.NOT_FOUND
                    if request.operation is Operation.READ
                    else DenialSurface.FORBIDDEN
                ),
            )

        await self._record(session, request, outcome, audit=audit)
        return outcome

    # ── 04 §3, step by step ─────────────────────────────────────────────

    async def _decide(
        self, session: AsyncSession, request: AccessRequest
    ) -> AuthorizationOutcome:
        principal = request.principal

        # D1 · MEMBERSHIP (04 §2 D1)
        #
        # Evaluated first because it is both cheapest and safest: failing here
        # short-circuits before any resource is loaded, so a non-member never
        # learns whether the resource they named exists.
        role: MembershipRole | None = None
        if request.graph_id is not None:
            role = await self._memberships.active_role(
                session, graph_id=request.graph_id, user_id=principal.user_id
            )
            if role is None:
                return _deny("not_a_member", DenialSurface.NOT_FOUND)

        # Resolve the resource (everything but `create`).
        resource: ResourceDescriptor | None = None
        if request.operation is not Operation.CREATE:
            if request.resource_ref is None:
                return _deny("missing_resource_ref", DenialSurface.NOT_FOUND)
            resource = await self._resources.load(
                session, request.resource_type, str(request.resource_ref)
            )
            if resource is None:
                return _deny("not_found", DenialSurface.NOT_FOUND)

        # D2 · ROLE (04 §2 D2)
        if not _role_permits(role=role, operation=request.operation):
            # 04 §7: this is a denial about an operation on a graph the caller
            # is already a member of, so no existence is leaked and 403 is
            # correct.
            return _deny("role_forbids", DenialSurface.FORBIDDEN)

        # D4 is needed before D3 can safely return 403: a caller who cannot see
        # the resource at all must not learn that it exists by being told they
        # are "not the owner". So visibility is resolved first and D3's surface
        # depends on it.
        is_member_of_resource_graph = False
        if resource is not None and resource.graph_id is not None:
            if resource.graph_id == request.graph_id and role is not None:
                is_member_of_resource_graph = True
            else:
                is_member_of_resource_graph = (
                    await self._memberships.active_role(
                        session, graph_id=resource.graph_id, user_id=principal.user_id
                    )
                    is not None
                )

        visible = resource is None or readable(
            user_id=principal.user_id,
            resource=resource,
            is_active_member_of_resource_graph=is_member_of_resource_graph,
        )

        if not visible:
            # D4 · VISIBILITY (04 §2 D4, RAUTH-003) — the rule that prevents
            # cross-user leakage inside a shared graph. AZ-T1, the single most
            # important test in Track B, lands here: 404, so a private resource
            # is indistinguishable from an absent one (04 §7).
            return _deny("not_visible", DenialSurface.NOT_FOUND)

        # D3 · OWNERSHIP (04 §2 D3)
        if resource is not None and request.operation in _OWNER_ONLY_OPERATIONS:
            if resource.owner_user_id != principal.user_id:
                return _deny("owner_only", DenialSurface.FORBIDDEN)

        # D5 · CAPABILITY (04 §2 D5)
        #
        # `[LOCKED]` (04 §2 D5) checked *in addition to* D1–D4, never instead of
        # them — which is why it sits after the visibility gate above: a granted
        # capability cannot rescue a request that already failed D4 (AZ-T7).
        if request.required_capability:
            granted = await self._capabilities.has_capability(
                session,
                capability=request.required_capability,
                context=CapabilityCheckContext(
                    principal=principal,
                    graph_id=request.graph_id,
                    task_id=request.task_id,
                    resource_scope=request.resource_scope,
                ),
                capability_operation=request.capability_operation,
            )
            if not granted:
                return _deny("capability_missing", DenialSurface.FORBIDDEN)

        # ABSOLUTE FLOOR (PERM-006, 04 §2)
        #
        # Expected structurally unreachable — no registry entry describes a floor
        # action, so no grant for one exists to pass D5. Implemented anyway as
        # 04 §2's "defense-in-depth backstop", and reached *before* any risk tier
        # is computed so a prohibited action can never be weighed, tiered, or
        # offered as confirmable (PERM-007).
        floor_category = self._floor.floor_category_for_request(
            capability=request.required_capability,
            operation=request.operation,
            resource_type=request.resource_type,
        )
        if floor_category is not None:
            return AuthorizationOutcome(
                decision=PermissionDecisionValue.DENY,
                risk_category=RiskCategory.HIGH_IRREVERSIBLE,
                reason=f"prohibited:{floor_category}",
                surface=DenialSurface.PROHIBITED,
                floor_category=str(floor_category),
                resource=resource,
            )

        # SENSITIVE-APP GATE (docs/CAPABILITY_MATRIX.md §5.1, docs/23 §5.5)
        #
        # A device UI operation in an app the owner has not classified — or a
        # screenshot of any app not classified non-sensitive — is refused
        # outright, never offered for confirmation: "not classified" is not
        # permission. Checked after the floor (a prohibited action is never
        # weighed) and before the tier (which the classification may raise).
        scope_denial = self._risk.scope_denial(
            capability_name=request.required_capability,
            capability_operation=request.capability_operation,
            resource_scope=request.resource_scope,
        )
        if scope_denial is not None:
            return _deny(scope_denial, DenialSurface.FORBIDDEN)

        # RISK TIER (PERM-004/005)
        tier = self._risk.risk_tier(
            resource_type=request.resource_type,
            operation=request.operation,
            capability_name=request.required_capability,
            capability_operation=request.capability_operation,
            resource_scope=request.resource_scope,
        )

        if self._risk.requires_confirmation(tier):
            binding = _binding_for(request)
            if request.confirmation_token and await self._confirmations.consume(
                session, token=request.confirmation_token, binding=binding
            ):
                return AuthorizationOutcome(
                    decision=PermissionDecisionValue.ALLOW,
                    risk_category=tier,
                    reason="allowed_with_confirmation",
                    resource=resource,
                )
            # No token, or a token that does not match this exact action: the
            # action is *not* performed. An expired or already-spent token lands
            # here too — 05 §4's "no timeout auto-approves" is this branch
            # having no path to `ALLOW`.
            return AuthorizationOutcome(
                decision=PermissionDecisionValue.REQUIRE_CONFIRMATION,
                risk_category=tier,
                reason="confirmation_required",
                confirmation_required_for=binding,
                resource=resource,
            )

        return AuthorizationOutcome(
            decision=PermissionDecisionValue.ALLOW,
            risk_category=tier,
            reason="allowed",
            resource=resource,
        )

    # ── audit (04 §1/§3, AZ-T12) ────────────────────────────────────────

    async def _record(
        self,
        session: AsyncSession,
        request: AccessRequest,
        outcome: AuthorizationOutcome,
        *,
        audit: AuditLogger,
    ) -> None:
        resource_ref = (
            str(request.resource_ref)
            if request.resource_ref is not None
            else f"{request.resource_type.value}:*"
        )

        await audit.record_permission_decision(
            principal_id=request.principal.user_id,
            capability=request.required_capability,
            resource_ref=resource_ref,
            decision=outcome.decision,
            risk_category=outcome.risk_category,
            reason=outcome.reason,
        )

        await audit.record(
            actor=AuditActor.USER,
            action=AuditAction.AUTHORIZATION_DECIDED,
            resource=f"{request.resource_type.value}:{resource_ref}",
            result=(
                AuditResult.SUCCESS
                if outcome.allowed
                else AuditResult.BLOCKED
            ),
            decision=outcome.decision,
            user_id=request.principal.user_id,
            device_id=request.principal.device_id,
            session_id=request.principal.session_id,
            graph_id=request.graph_id,
        )


def _deny(reason: str, surface: DenialSurface) -> AuthorizationOutcome:
    return AuthorizationOutcome(
        decision=PermissionDecisionValue.DENY,
        risk_category=RiskCategory.LOW_READ,
        reason=reason,
        surface=surface,
    )


def _role_permits(*, role: MembershipRole | None, operation: Operation) -> bool:
    """D2 (04 §2).

    Roles gate *graph-management* operations, not resource-content operations —
    content is governed by D3/D4. So the only thing this returns `False` for is
    an `administer` operation attempted by a non-owner.

    `role is None` means the request had no graph context (a personal resource,
    04 §3: "graph_id null → personal resource; membership implicit via ownership
    below"). Administering has no meaning without a graph, so it is refused.
    """

    if operation in _OWNER_ROLE_OPERATIONS:
        return role is MembershipRole.OWNER

    if role is None:
        return True

    if role in {MembershipRole.OWNER, MembershipRole.MEMBER}:
        return True

    # An unrecognised role denies (fail-closed). Reached only if the role enum
    # grows (OD-E1) without this function being revisited — exactly when a
    # permissive default would be most dangerous.
    return False


def _binding_for(request: AccessRequest) -> ActionBinding:
    """Build the confirmation binding for this exact action (PERM-004).

    `task_id` falls back to the resource reference when a request has no agent
    task: a confirmation still has to be bound to *something* stable, and
    binding to the resource keeps a token minted for one resource from
    validating against another.
    """

    return ActionBinding(
        principal_user_id=request.principal.user_id,
        session_id=request.principal.session_id,
        task_id=request.task_id or f"{request.resource_type.value}:{request.resource_ref}",
        capability=request.required_capability,
        operation=request.operation,
        resource_type=request.resource_type,
        resource_ref=str(request.resource_ref) if request.resource_ref is not None else None,
        arguments=request.arguments,
    )
