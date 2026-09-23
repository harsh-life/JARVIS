"""Graph lifecycle and the explicit visibility change (04 §4, §5, §6).

Every mutation here goes through the authorization engine or enforces the
owner-only rule the engine would enforce, and every one is audited. The
separation from `repository.py` is deliberate: the module the engine calls on
its D1 hot path has no write path at all.

`[LOCKED]` rules realized here:
- membership is created **only** by an owner's explicit approval — never
  self-service, never by the requester asserting it (04 §4.2);
- revocation is immediate: the next check fails D1 (04 §4.3, AZ-T8);
- sharing is owner-only, explicit, and audited — **never** a side effect of any
  other operation (04 §5, RAUTH-005, RAUTH V2);
- a graph always has exactly one owner; transfer is atomic (04 §6).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.graph.repository import GraphRepository, utcnow
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.storage.models import (
    FileResource,
    Graph,
    GraphAccessRequest,
    GraphMembership,
    ScheduledJob,
)
from shared.schemas.enums import (
    AuditActor,
    AuditResult,
    GraphType,
    MembershipRole,
    Visibility,
)


class GraphOperationRefused(Exception):
    """A lifecycle operation was refused.

    `surface_as_not_found` carries 04 §7's distinction outward: a refusal that
    would disclose a graph or membership the caller cannot see is surfaced as
    `404`, never as a distinguishable `403`.
    """

    def __init__(self, reason: str, *, surface_as_not_found: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.surface_as_not_found = surface_as_not_found


@dataclass(frozen=True)
class VisibilityChange:
    resource_type: str
    resource_ref: uuid.UUID
    previous: Visibility
    current: Visibility


class GraphService:
    def __init__(self, *, repository: GraphRepository | None = None) -> None:
        self._repo = repository or GraphRepository()

    # ── 04 §4.1 create ──────────────────────────────────────────────────

    async def create_graph(
        self,
        session: AsyncSession,
        *,
        name: str,
        graph_type: GraphType,
        creator_user_id: uuid.UUID,
        audit: AuditLogger,
    ) -> Graph:
        """GRAPH-007: the creator becomes owner, with a membership row.

        The owner's `GraphMembership` is created in the same transaction as the
        graph. A graph whose owner had no membership row would fail D1 for its
        own owner — so these are never two separate steps that could half-apply.
        """

        graph = Graph(
            name=name,
            owner_user_id=creator_user_id,
            type=graph_type,
            created_at=utcnow(),
        )
        session.add(graph)
        await session.flush()

        session.add(
            GraphMembership(
                graph_id=graph.graph_id,
                user_id=creator_user_id,
                role=MembershipRole.OWNER,
                granted_by=creator_user_id,
                granted_at=utcnow(),
            )
        )
        await session.flush()

        await audit.record(
            actor=AuditActor.USER,
            action=AuditAction.GRAPH_CREATED,
            resource=f"graph:{graph.graph_id}",
            result=AuditResult.SUCCESS,
            user_id=creator_user_id,
            graph_id=graph.graph_id,
        )
        return graph

    # ── 04 §4.2 request → approval ──────────────────────────────────────

    async def request_access(
        self,
        session: AsyncSession,
        *,
        graph_id: uuid.UUID,
        requester_user_id: uuid.UUID,
        message: str | None,
        audit: AuditLogger,
    ) -> GraphAccessRequest:
        """GRAPH-008: ask an owner for membership. Grants nothing by itself.

        A request against a `private` graph is refused as `404`: a private graph
        is not joinable, and saying so distinguishably would confirm it exists
        (04 §7, 04 §4.1 "a `private` graph has exactly one membership").
        """

        graph = await self._repo.get_graph(session, graph_id)
        if graph is None or graph.type is not GraphType.SHARED:
            raise GraphOperationRefused("not_found", surface_as_not_found=True)

        if await self._repo.is_active_member(
            session, graph_id=graph_id, user_id=requester_user_id
        ):
            raise GraphOperationRefused("already_a_member")

        existing = await session.execute(
            select(GraphAccessRequest).where(
                GraphAccessRequest.graph_id == graph_id,
                GraphAccessRequest.user_id == requester_user_id,
                GraphAccessRequest.status == "pending",
            )
        )
        pending = existing.scalars().first()
        if pending is not None:
            return pending

        row = GraphAccessRequest(
            graph_id=graph_id,
            user_id=requester_user_id,
            message=message,
            status="pending",
            created_at=utcnow(),
        )
        session.add(row)
        await session.flush()

        await audit.record(
            actor=AuditActor.USER,
            action=AuditAction.GRAPH_ACCESS_REQUESTED,
            resource=f"graph_access_request:{row.request_id}",
            result=AuditResult.SUCCESS,
            user_id=requester_user_id,
            graph_id=graph_id,
        )
        return row

    async def approve_member(
        self,
        session: AsyncSession,
        *,
        graph_id: uuid.UUID,
        approver_user_id: uuid.UUID,
        user_id: uuid.UUID,
        role: MembershipRole,
        audit: AuditLogger,
    ) -> GraphMembership:
        """04 §4.2: membership is created **only** by an owner's approval.

        The caller's owner role is re-derived here from the membership table
        rather than taken from the request — this is the operation PHONE-003
        most obviously applies to, since a client that could assert "I am the
        owner" would be able to add itself to any graph.

        `[LOCKED]` (04 §6) a graph always has exactly one owner, so approving a
        second `owner` role is refused; ownership changes go through
        `transfer_ownership`.
        """

        approver_role = await self._repo.active_role(
            session, graph_id=graph_id, user_id=approver_user_id
        )
        if approver_role is None:
            # The approver is not even a member: do not confirm the graph exists.
            raise GraphOperationRefused("not_found", surface_as_not_found=True)
        if approver_role is not MembershipRole.OWNER:
            raise GraphOperationRefused("owner_only")

        if role is MembershipRole.OWNER:
            raise GraphOperationRefused("single_owner_invariant")

        # 04 §4.1 `[LOCKED]`: "a `private` graph has exactly one membership (its
        # owner)". Approval is the only way membership grows, so it is the one
        # place that invariant has to hold.
        graph = await self._repo.get_graph(session, graph_id)
        if graph is None or graph.type is not GraphType.SHARED:
            raise GraphOperationRefused("private_graph_not_joinable")

        if await self._repo.is_active_member(session, graph_id=graph_id, user_id=user_id):
            raise GraphOperationRefused("already_a_member")

        # GRAPH-008 / 04 §4.2: membership is "explicit owner approval of a join
        # request" — the owner answers a request the joining user made. Without
        # a pending request an owner could enrol anyone without their consent,
        # making another user's context a target for graph-shared content.
        pending = await session.execute(
            select(GraphAccessRequest).where(
                GraphAccessRequest.graph_id == graph_id,
                GraphAccessRequest.user_id == user_id,
                GraphAccessRequest.status == "pending",
            )
        )
        request_row = pending.scalars().first()
        if request_row is None:
            raise GraphOperationRefused("no_pending_access_request")

        membership = GraphMembership(
            graph_id=graph_id,
            user_id=user_id,
            role=role,
            granted_by=approver_user_id,
            granted_at=utcnow(),
        )
        session.add(membership)

        request_row.status = "approved"
        request_row.decided_at = utcnow()
        request_row.decided_by = approver_user_id

        await session.flush()

        await audit.record(
            actor=AuditActor.USER,
            action=AuditAction.MEMBERSHIP_APPROVED,
            resource=f"membership:{membership.membership_id}",
            result=AuditResult.SUCCESS,
            user_id=approver_user_id,
            graph_id=graph_id,
        )
        return membership

    # ── 04 §4.3 leave / revoke ──────────────────────────────────────────

    async def revoke_membership(
        self,
        session: AsyncSession,
        *,
        graph_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        target_user_id: uuid.UUID,
        audit: AuditLogger,
    ) -> GraphMembership:
        """04 §4.3: a member may leave; an owner may revoke any member.

        Effect is immediate — the row's `revoked_at` is what `active_role`
        filters on, and that is re-read live on every check (AZ-T8).

        The graph's own owner cannot be revoked: that would leave a graph with
        zero owners, breaking 04 §6's invariant. Ownership is transferred, and
        deleting the graph is the owner's exit.

        `[OPEN — OWNER] OD-AUTHZ-1` — 04 §4.3 leaves open whether a leaver's
        graph-shared resources stay shared or auto-unshare, recommending
        "stay". This implementation does what the recommendation says (it
        touches no resource visibility on revocation) and does **not** treat
        that as settled; the owner's decision is still pending.
        """

        actor_role = await self._repo.active_role(
            session, graph_id=graph_id, user_id=actor_user_id
        )
        if actor_role is None:
            raise GraphOperationRefused("not_found", surface_as_not_found=True)

        if actor_user_id != target_user_id and actor_role is not MembershipRole.OWNER:
            raise GraphOperationRefused("owner_only")

        result = await session.execute(
            select(GraphMembership).where(
                GraphMembership.graph_id == graph_id,
                GraphMembership.user_id == target_user_id,
                GraphMembership.revoked_at.is_(None),
            )
        )
        membership = result.scalars().first()
        if membership is None:
            raise GraphOperationRefused("not_found", surface_as_not_found=True)

        if membership.role is MembershipRole.OWNER:
            raise GraphOperationRefused("single_owner_invariant")

        membership.revoked_at = utcnow()
        await session.flush()

        await audit.record(
            actor=AuditActor.USER,
            action=AuditAction.MEMBERSHIP_REVOKED,
            resource=f"membership:{membership.membership_id}",
            result=AuditResult.SUCCESS,
            user_id=actor_user_id,
            graph_id=graph_id,
        )
        return membership

    # ── 04 §6 ownership transfer ────────────────────────────────────────

    async def transfer_ownership(
        self,
        session: AsyncSession,
        *,
        graph_id: uuid.UUID,
        current_owner_user_id: uuid.UUID,
        new_owner_user_id: uuid.UUID,
        audit: AuditLogger,
    ) -> Graph:
        """04 §6: owner-only, atomic, exactly one owner at all times.

        `[IMPL]` (OD-AUTHZ-2) immediate rather than accept-required — the
        simpler of the two the doc leaves open, and the one that cannot leave a
        graph in a pending-ownership state. Both role changes and the graph's
        `owner_user_id` move inside one transaction, so there is no window with
        zero or two owners.

        `[LOCKED]` (04 §6) resource ownership does **not** transfer with graph
        ownership: nothing here touches any resource's `owner_user_id`, so a new
        graph owner does not thereby own members' resources.
        """

        graph = await self._repo.get_graph(session, graph_id)
        if graph is None:
            raise GraphOperationRefused("not_found", surface_as_not_found=True)

        current_role = await self._repo.active_role(
            session, graph_id=graph_id, user_id=current_owner_user_id
        )
        if current_role is None:
            raise GraphOperationRefused("not_found", surface_as_not_found=True)
        if current_role is not MembershipRole.OWNER:
            raise GraphOperationRefused("owner_only")

        memberships = {
            m.user_id: m for m in await self._repo.active_memberships(session, graph_id=graph_id)
        }
        new_owner_membership = memberships.get(new_owner_user_id)
        if new_owner_membership is None:
            # Transfer targets an existing member only (04 §6 "transfer graph
            # ownership to another member"), and a non-member is reported as
            # absent rather than confirmed as a non-member.
            raise GraphOperationRefused("not_found", surface_as_not_found=True)

        new_owner_membership.role = MembershipRole.OWNER
        memberships[current_owner_user_id].role = MembershipRole.MEMBER
        graph.owner_user_id = new_owner_user_id
        await session.flush()

        await audit.record(
            actor=AuditActor.USER,
            action=AuditAction.GRAPH_OWNERSHIP_TRANSFERRED,
            resource=f"graph:{graph_id}",
            result=AuditResult.SUCCESS,
            user_id=current_owner_user_id,
            graph_id=graph_id,
        )
        return graph

    # ── 04 §5 the explicit visibility change ────────────────────────────

    async def change_visibility(
        self,
        session: AsyncSession,
        *,
        resource_type: str,
        resource_ref: uuid.UUID,
        actor_user_id: uuid.UUID,
        new_visibility: Visibility,
        audit: AuditLogger,
    ) -> VisibilityChange:
        """04 §5 / RAUTH V2: owner-only, explicit, audited, reversible.

        This is the **only** function in the codebase that writes a
        `visibility` field. That is the structural form of RAUTH-005's "never a
        side effect of any other operation": there is no other writer to audit,
        review, or forget to audit.

        Ownership is checked here as well as by the engine, because this method
        is callable in-process by later branches and the owner-only rule must
        not depend on the caller having remembered to authorize first.

        A `graph`-visibility change requires the resource to have a `graph_id`:
        making something "graph-visible" with no graph would be a resource whose
        visibility names an audience that does not exist.
        """

        row, visibility_attr, graph_attr, owner_attr = await _load_visibility_bearing(
            session, resource_type, resource_ref
        )
        if row is None:
            raise GraphOperationRefused("not_found", surface_as_not_found=True)

        if getattr(row, owner_attr) != actor_user_id:
            # Not the owner: do not disclose that the resource exists unless the
            # caller could already see it. Surfacing as not-found is the safe
            # side of 04 §7 for a caller we have not visibility-checked here.
            raise GraphOperationRefused("owner_only", surface_as_not_found=True)

        previous: Visibility = getattr(row, visibility_attr) or Visibility.PRIVATE

        if new_visibility is Visibility.GRAPH and getattr(row, graph_attr) is None:
            raise GraphOperationRefused("no_graph_scope")

        setattr(row, visibility_attr, new_visibility)
        await session.flush()

        await audit.record(
            actor=AuditActor.USER,
            action=(
                AuditAction.RESOURCE_SHARED
                if new_visibility is Visibility.GRAPH
                else AuditAction.RESOURCE_UNSHARED
            ),
            resource=f"{resource_type}:{resource_ref}",
            result=AuditResult.SUCCESS,
            user_id=actor_user_id,
            graph_id=getattr(row, graph_attr),
        )

        return VisibilityChange(
            resource_type=resource_type,
            resource_ref=resource_ref,
            previous=previous,
            current=new_visibility,
        )


# The visibility-bearing tables this branch can reach. `mem0fact` is absent
# because Mem0 is `11`'s store, not a relational table (see
# server/graph/resources.py's note); that branch calls `change_visibility` for
# its own store through the same owner-only, audited path.
_VISIBILITY_BEARING = {
    "fileresource": (FileResource, "visibility", "graph_id", "owner_user_id", "file_id"),
    "scheduledjob": (ScheduledJob, "visibility", "graph_id", "owner_user_id", "job_id"),
}


async def _load_visibility_bearing(
    session: AsyncSession, resource_type: str, resource_ref: uuid.UUID
):
    spec = _VISIBILITY_BEARING.get(resource_type)
    if spec is None:
        raise GraphOperationRefused("unsupported_resource_type")
    model, visibility_attr, graph_attr, owner_attr, _pk = spec
    row = await session.get(model, resource_ref)
    return row, visibility_attr, graph_attr, owner_attr
