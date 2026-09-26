"""Projecting stored rows down to their authorization facts (04 §1).

A `ResourceLoader` answers one question — "what are this resource's
authorization-relevant facts?" — and deliberately cannot answer any other. It
returns a `ResourceDescriptor` (owner, visibility, graph scoping) and never the
resource's content, so the engine has nothing to base a decision on except the
five dimensions.

`[LOCKED]` (04 §9) "a resource with `graph_id` set but `visibility` missing is
treated as `private`". Every projection below applies that default explicitly
rather than passing a null through.

**Which resource types this branch can load.** Security Core owns no resource
subsystem: `mem0fact` belongs to `11`, and the filesystem behind `fileresource`
belongs to `09`. What exists here is the *row-level* projection for the tables
foundation already created, which is exactly what the engine needs to be
testable against real multi-user fixtures (17 §6). An unregistered resource type
returns `None`, which the engine surfaces as `404` — fail-closed, and the right
answer for a subsystem that does not exist yet rather than an accidental allow.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from server.graph.ports import ResourceDescriptor, ResourceLoader
from server.storage.models import (
    AgentConfiguration,
    CapabilityGrant,
    FileResource,
    Graph,
    GraphMembership,
    ScheduledJob,
    SecretReference,
)
from shared.schemas.authorization import ResourceType
from shared.schemas.enums import Visibility


class SecurityCoreResourceLoader:
    """Structurally satisfies `server.graph.ports.ResourceLoader`.

    Later branches compose rather than replace: a subsystem registers its own
    loader for its own resource type, and none of them re-implement the
    authorization algorithm (§19 of this branch's scope — "a future tool should
    ask deterministic Security Core, not implement its own authorization").
    """

    def __init__(self) -> None:
        self._external: dict[ResourceType, ResourceLoader] = {}

    def register(self, resource_type: ResourceType, loader: ResourceLoader) -> None:
        """Add the loader for a resource type whose store lives outside this
        layer (`mem0fact` — `11`'s Mem0 store). Additive only: a type this
        module already loads, or one already registered, is refused, so no later
        branch can substitute the projection another type's decisions rest on."""

        if resource_type in _LOADERS or resource_type in self._external:
            raise ValueError(f"a loader for {resource_type.value!r} already exists")
        self._external[resource_type] = loader

    async def load(
        self,
        session: AsyncSession,
        resource_type: ResourceType,
        resource_ref: str,
    ) -> ResourceDescriptor | None:
        external = self._external.get(resource_type)
        if external is not None:
            descriptor = await external.load(session, resource_type, resource_ref)
            # A loader answers only for its own type (fail-closed otherwise).
            if descriptor is not None and descriptor.resource_type is not resource_type:
                return None
            return descriptor
        loader = _LOADERS.get(resource_type)
        if loader is None:
            return None

        try:
            parsed = uuid.UUID(str(resource_ref))
        except (ValueError, AttributeError, TypeError):
            # A malformed reference is not loadable. Fail-closed: the engine
            # turns `None` into a 404-surfaced denial rather than treating an
            # unparseable id as a wildcard.
            if resource_type is not ResourceType.SECRET_REFERENCE:
                return None
            parsed = None  # secret handles are opaque strings, not UUIDs

        return await loader(session, resource_ref, parsed)


async def _load_graph(
    session: AsyncSession, resource_ref: str, parsed: uuid.UUID | None
) -> ResourceDescriptor | None:
    row = await session.get(Graph, parsed)
    if row is None:
        return None
    # A graph is readable by its active members, which is exactly what
    # `visibility: graph` plus D1 expresses. Its owner is the graph's owner.
    return ResourceDescriptor(
        resource_type=ResourceType.GRAPH,
        resource_ref=str(row.graph_id),
        owner_user_id=row.owner_user_id,
        visibility=Visibility.GRAPH,
        graph_id=row.graph_id,
        source_user_id=row.owner_user_id,
    )


async def _load_membership(
    session: AsyncSession, resource_ref: str, parsed: uuid.UUID | None
) -> ResourceDescriptor | None:
    row = await session.get(GraphMembership, parsed)
    if row is None:
        return None
    # The "owner" of a membership is the member it describes — so a member can
    # read and revoke their own membership (04 §4.3 "a member may leave"),
    # while administering *someone else's* needs the owner role, which D2
    # enforces.
    return ResourceDescriptor(
        resource_type=ResourceType.MEMBERSHIP,
        resource_ref=str(row.membership_id),
        owner_user_id=row.user_id,
        visibility=Visibility.GRAPH,
        graph_id=row.graph_id,
        source_user_id=row.granted_by,
    )


async def _load_file(
    session: AsyncSession, resource_ref: str, parsed: uuid.UUID | None
) -> ResourceDescriptor | None:
    row = await session.get(FileResource, parsed)
    if row is None:
        return None
    return ResourceDescriptor(
        resource_type=ResourceType.FILERESOURCE,
        resource_ref=str(row.file_id),
        owner_user_id=row.owner_user_id,
        visibility=row.visibility or Visibility.PRIVATE,
        graph_id=row.graph_id,
        source_user_id=row.source_user_id,
    )


async def _load_job(
    session: AsyncSession, resource_ref: str, parsed: uuid.UUID | None
) -> ResourceDescriptor | None:
    row = await session.get(ScheduledJob, parsed)
    if row is None:
        return None
    return ResourceDescriptor(
        resource_type=ResourceType.SCHEDULEDJOB,
        resource_ref=str(row.job_id),
        owner_user_id=row.owner_user_id,
        visibility=row.visibility or Visibility.PRIVATE,
        graph_id=row.graph_id,
        source_user_id=row.source_user_id,
    )


async def _load_capability_grant(
    session: AsyncSession, resource_ref: str, parsed: uuid.UUID | None
) -> ResourceDescriptor | None:
    row = await session.get(CapabilityGrant, parsed)
    if row is None:
        return None
    # A grant is private to the principal it applies to. It is never
    # graph-visible: one member being able to read another's grants would
    # disclose what the other user has authorized their agent to do.
    return ResourceDescriptor(
        resource_type=ResourceType.CAPABILITY_GRANT,
        resource_ref=str(row.grant_id),
        owner_user_id=row.principal_id,
        visibility=Visibility.PRIVATE,
        graph_id=None,
        source_user_id=row.granted_by,
    )


async def _load_agent_config(
    session: AsyncSession, resource_ref: str, parsed: uuid.UUID | None
) -> ResourceDescriptor | None:
    row = await session.get(AgentConfiguration, parsed)
    if row is None:
        return None
    # 01 §4.1: a config is scoped by `(scope_type, scope_id)`. For a
    # user-scoped config the scope id *is* the owning user; for a graph-scoped
    # one, administering it is owner-only (04 §2 D2) and the graph is its
    # scope, so the graph's own owner check applies through D2 rather than
    # through a resource owner here.
    return ResourceDescriptor(
        resource_type=ResourceType.AGENTCONFIG,
        resource_ref=str(row.config_id),
        owner_user_id=row.scope_id,
        visibility=Visibility.PRIVATE,
        graph_id=row.scope_id if row.scope_type.value == "graph" else None,
        source_user_id=row.scope_id,
    )


async def _load_secret_reference(
    session: AsyncSession, resource_ref: str, parsed: uuid.UUID | None
) -> ResourceDescriptor | None:
    """Secret references load only so the engine can *refuse* them.

    `floor_category_for_request` classifies every mutating operation on a
    secret reference as absolute-floor (GRAPH-009, AZ-T5), so this projection
    exists to make the refusal a decision about a real resource rather than a
    404 that looks like a typo. `owner_user_id` is deliberately the scope id
    and `visibility` is always `private` — a secret is never graph-visible by
    any path (12 §2).
    """

    row = await session.get(SecretReference, resource_ref)
    if row is None:
        return None

    owner: uuid.UUID
    try:
        owner = uuid.UUID(str(row.owner_scope_id))
    except (ValueError, TypeError):
        # A server-scoped secret has no owning user (01 §8: "Null for `server`
        # scope"). The nil UUID matches no principal, so ownership can never
        # be satisfied for it — which is correct: server-scoped secrets resolve
        # only through the SecretStore's own mediation, never this path.
        owner = uuid.UUID(int=0)

    return ResourceDescriptor(
        resource_type=ResourceType.SECRET_REFERENCE,
        resource_ref=row.secret_ref,
        owner_user_id=owner,
        visibility=Visibility.PRIVATE,
        graph_id=None,
        source_user_id=None,
    )


_LOADERS = {
    ResourceType.GRAPH: _load_graph,
    ResourceType.MEMBERSHIP: _load_membership,
    ResourceType.FILERESOURCE: _load_file,
    ResourceType.SCHEDULEDJOB: _load_job,
    ResourceType.CAPABILITY_GRANT: _load_capability_grant,
    ResourceType.AGENTCONFIG: _load_agent_config,
    ResourceType.SECRET_REFERENCE: _load_secret_reference,
    # ResourceType.MEM0FACT is absent on purpose: Mem0 is `11`'s store, not a
    # relational table this layer may reach into. The composition root registers
    # its loader (`server/composition/memory.py`) when a memory provider is
    # configured; without one, a mem0fact request is unloadable and denied (04 §9).
}
