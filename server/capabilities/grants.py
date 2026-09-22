"""CapabilityGrant lifecycle and the D5 capability check (01 §7.1, 07 §2).

The chain PRD §16 locks:

    explicit grant → capability → resource scope → tool/operation → primitive

This module owns the first two links and the check the authorization engine
runs for D5. Three properties it exists to guarantee:

* **A model cannot manufacture, modify, or grant itself a capability.** Grants
  are created only here, only from an authenticated user's consent
  (`granted_by`, PERM-002), and `server.agent` cannot import this package at
  all (pyproject's "Agent cannot import the capability/authz engine"
  contract). There is no self-grant path to close because there is no path.

* **`AgentConfiguration` is never authority.** `01` §4.1 gives
  `agent_configurations.granted_capabilities` as *"the resolved set"*, and
  §7.1 names `CapabilityGrant` *"the authoritative record behind
  AgentConfiguration's resolved set"*. So this module reads **only**
  `capability_grants`. Nothing here consults an `AgentConfiguration`, and
  `has_capability` cannot be satisfied by one — a config describes what is
  configured; authority comes from the grant table alone.

* **A grant never bypasses visibility.** `has_capability` answers exactly one
  question — "is this capability granted in this scope?" — and returns a bool.
  It has no access to a resource, so it *cannot* be mistaken for permission to
  read one (07 §2, 04 §2 D5, AZ-T7/TL-T3). D4 runs independently in
  `server/graph/authorization.py`.

Scope semantics follow `01` §7.1 verbatim: `principal_id` is *"the
user/device/session/graph/task the grant applies to (per `scope_type`)"* —
i.e. the id **is** the scope's id, not always a user id. 02 §6's request field
`scope_id` is that same value under the client's name for it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.capabilities.floor import AbsoluteFloorViolation, assert_not_absolute_floor
from server.capabilities.registry import (
    UnknownCapability,
    UnknownOperation,
    lookup,
    validate_resource_scope,
)
from server.storage.models import CapabilityGrant
from shared.schemas.authorization import CapabilityCheckContext
from shared.schemas.enums import CapabilityScopeType


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CapabilityGrantRefused(Exception):
    """A grant could not be created. Never a stored-but-denied grant (01 §7.1)."""


class CapabilityGrantService:
    """Structurally satisfies `server.graph.ports.CapabilityChecker`.

    No inheritance, and no import of `server.graph`: the two modules are
    mutually independent by design (16 §5), so the engine consumes this
    through a Protocol and the gateway wires them together.
    """

    async def grant(
        self,
        session: AsyncSession,
        *,
        principal_id: uuid.UUID,
        scope_type: CapabilityScopeType,
        capability: str,
        granted_by: uuid.UUID,
        resource_scope: Mapping[str, str] | None = None,
        expires_at: datetime | None = None,
    ) -> CapabilityGrant:
        """Create a grant from explicit user consent (PERM-002).

        Refusal order matters: the absolute-floor classification runs *first*,
        so an escalation attempt is audited as the prohibition it is rather
        than as an unknown-capability typo (see `floor.py`).
        """

        try:
            assert_not_absolute_floor(capability)
        except AbsoluteFloorViolation as exc:
            # 01 §7.1 [LOCKED]: "attempting to create one is a hard error, not
            # a stored-but-denied grant." Nothing is written on this path.
            raise exc

        try:
            lookup(capability)
            validate_resource_scope(capability, resource_scope)
        except (UnknownCapability, UnknownOperation) as exc:
            raise CapabilityGrantRefused(str(exc)) from exc

        if expires_at is not None and expires_at <= _utcnow():
            # An already-expired grant would be a confusing no-op row that
            # looks like authority in the dashboard but grants nothing.
            raise CapabilityGrantRefused("expires_at must be in the future")

        row = CapabilityGrant(
            principal_id=principal_id,
            scope_type=scope_type,
            capability=capability,
            resource_scope=dict(resource_scope) if resource_scope else None,
            granted_by=granted_by,
            created_at=_utcnow(),
            expires_at=expires_at,
        )
        session.add(row)
        await session.flush()
        return row

    async def revoke(
        self, session: AsyncSession, *, grant_id: uuid.UUID, revoked_by: uuid.UUID
    ) -> CapabilityGrant | None:
        """Revoke immediately (PRD §13: "the next device operation for that app
        fails authorization").

        Only the grant's own principal or its grantor may revoke, and a grant
        belonging to neither is reported as absent rather than forbidden — a
        caller must not be able to confirm another principal's grant exists by
        trying to revoke it (04 §7 anti-enumeration).
        """

        row = await session.get(CapabilityGrant, grant_id)
        if row is None:
            return None
        if revoked_by not in {row.principal_id, row.granted_by}:
            return None
        if row.revoked_at is None:
            row.revoked_at = _utcnow()
            await session.flush()
        return row

    async def list_active(
        self, session: AsyncSession, *, principal_ids: set[uuid.UUID]
    ) -> list[CapabilityGrant]:
        """The caller's active grants (02 §6 `GET /api/v1/capabilities`)."""

        now = _utcnow()
        result = await session.execute(
            select(CapabilityGrant).where(
                CapabilityGrant.principal_id.in_(principal_ids),
                CapabilityGrant.revoked_at.is_(None),
            )
        )
        return [row for row in result.scalars().all() if not _is_expired(row, now)]

    async def has_capability(
        self,
        session: AsyncSession,
        *,
        capability: str,
        context: CapabilityCheckContext,
        capability_operation: str | None = None,
    ) -> bool:
        """D5 (04 §2): is `capability` actively granted in this context?

        Returns a plain bool and touches no resource, so it can never be
        mistaken for — or substituted for — the visibility check.

        Fail-closed at every uncertainty: an unregistered capability, an
        operation outside the capability's enumerated mapping (07 §3, TL-T8),
        an unrecognised scope type, or a narrowing the operation cannot prove
        it stays inside, all return `False`.
        """

        if not capability:
            return False

        # A floor-shaped capability is never granted, so it can never be
        # active. Checked here too rather than relying on grant-time refusal
        # alone: a row written before this branch existed, or by a direct
        # database edit, must still not authorize anything.
        try:
            assert_not_absolute_floor(capability)
            definition = lookup(capability)
            if capability_operation is not None:
                definition.risk_for(capability_operation)
        except (AbsoluteFloorViolation, UnknownCapability, UnknownOperation):
            return False

        now = _utcnow()
        candidate_ids = _candidate_principal_ids(context)
        if not candidate_ids:
            return False

        result = await session.execute(
            select(CapabilityGrant).where(
                CapabilityGrant.capability == capability,
                CapabilityGrant.principal_id.in_(candidate_ids),
                CapabilityGrant.revoked_at.is_(None),
            )
        )

        for row in result.scalars().all():
            if _is_expired(row, now):
                continue
            if not _scope_matches(row, context):
                continue
            if not _resource_scope_satisfied(row, context):
                continue
            return True

        return False


def _is_expired(row: CapabilityGrant, now: datetime) -> bool:
    if row.expires_at is None:
        return False
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= now


def _candidate_principal_ids(context: CapabilityCheckContext) -> set[uuid.UUID]:
    """The ids a grant could legitimately be keyed on for this context.

    Narrowing the SQL to these means a grant belonging to an unrelated
    principal is never even a candidate for the scope check below.
    """

    ids = {
        context.principal.user_id,
        context.principal.device_id,
        context.principal.session_id,
    }
    if context.graph_id is not None:
        ids.add(context.graph_id)
    return ids


def _scope_matches(row: CapabilityGrant, context: CapabilityCheckContext) -> bool:
    """01 §7.1: `principal_id` is the id of whatever `scope_type` names."""

    scope = row.scope_type

    if scope is CapabilityScopeType.USER:
        return row.principal_id == context.principal.user_id
    if scope is CapabilityScopeType.DEVICE:
        return row.principal_id == context.principal.device_id
    if scope is CapabilityScopeType.SESSION:
        return row.principal_id == context.principal.session_id
    if scope is CapabilityScopeType.GRAPH:
        # A graph-scoped grant applies only inside that graph. A request with
        # no graph context does not match one — the grant was consented to for
        # a graph, so using it outside would widen it.
        return context.graph_id is not None and row.principal_id == context.graph_id
    if scope is CapabilityScopeType.TASK:
        return context.task_id is not None and str(row.principal_id) == context.task_id

    # Unrecognised scope type: deny (FAIL-CORE-003). Reached only if the enum
    # grows without this function being updated, which is exactly when a
    # permissive default would be most dangerous.
    return False


def _resource_scope_satisfied(row: CapabilityGrant, context: CapabilityCheckContext) -> bool:
    """07 §2: a grant's `resource_scope` narrows it, and the narrowing binds.

    If a grant names a narrowing the operation does not declare a value for,
    the answer is `False`: the operation cannot prove it stays inside the scope
    the user consented to, and an unprovable claim is a denial rather than an
    assumption.
    """

    narrowing = row.resource_scope
    if not narrowing:
        return True

    declared = context.resource_scope or {}
    for key, expected in narrowing.items():
        if declared.get(key) != expected:
            return False
    return True
