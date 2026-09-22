"""Graph and membership reads for the authorization engine (04 §2 D1/D2).

Kept to *reads that authorization depends on*. The lifecycle mutations live in
`server/graph/service.py`, so the module the engine calls on its hot path has
no write path at all — D1 cannot accidentally create the membership it is
supposed to be checking for.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.storage.models import Graph, GraphMembership
from shared.schemas.enums import MembershipRole


class GraphRepository:
    """Structurally satisfies `server.graph.ports.MembershipReader`."""

    async def active_role(
        self, session: AsyncSession, *, graph_id: uuid.UUID, user_id: uuid.UUID
    ) -> MembershipRole | None:
        """D1 (04 §2): the user's **active** role in this graph, or `None`.

        `revoked_at IS NULL` is the whole of "active" (01 §3.2), and it is
        re-read live on every check rather than trusted from the session row —
        04 §9 requires a membership revoked mid-session to fail the *next*
        check, which only holds if this is a live query (AZ-T8).
        """

        result = await session.execute(
            select(GraphMembership.role).where(
                GraphMembership.graph_id == graph_id,
                GraphMembership.user_id == user_id,
                GraphMembership.revoked_at.is_(None),
            )
        )
        return result.scalars().first()

    async def is_active_member(
        self, session: AsyncSession, *, graph_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        return await self.active_role(session, graph_id=graph_id, user_id=user_id) is not None

    async def get_graph(self, session: AsyncSession, graph_id: uuid.UUID) -> Graph | None:
        return await session.get(Graph, graph_id)

    async def graphs_for_user(
        self, session: AsyncSession, *, user_id: uuid.UUID, limit: int = 50
    ) -> list[Graph]:
        """02 §4 `GET /api/v1/graphs` — "graphs the user is a member of".

        Scoped by the membership join, not filtered after the fact: a listing
        built from all graphs and then filtered is one refactor away from
        leaking the unfiltered set.
        """

        result = await session.execute(
            select(Graph)
            .join(GraphMembership, GraphMembership.graph_id == Graph.graph_id)
            .where(
                GraphMembership.user_id == user_id,
                GraphMembership.revoked_at.is_(None),
            )
            .order_by(Graph.created_at)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def active_memberships(
        self, session: AsyncSession, *, graph_id: uuid.UUID
    ) -> list[GraphMembership]:
        result = await session.execute(
            select(GraphMembership).where(
                GraphMembership.graph_id == graph_id,
                GraphMembership.revoked_at.is_(None),
            )
        )
        return list(result.scalars().all())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
