"""Context entities — Graph, GraphMembership.

Source: 01_DATA_MODEL_SCHEMA.md §3. Graph is Track B's first-class
authorization boundary (00_CANONICAL_PRD.md §11/§11A) — foundation
represents its shape only. The five-dimension authorization engine that
makes membership *meaningful* (RAUTH-001..005) is 04_AUTHORIZATION_GRAPH_RESOURCE.md's
job, explicitly out of scope here ("Foundation does NOT implement
security-sensitive business authorization").
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import Field

from shared.schemas.common import ORMBase, utcnow
from shared.schemas.enums import GraphType, MembershipRole


class Graph(ORMBase):
    """PRD GRAPH-001..009, §11A."""

    graph_id: UUID = Field(default_factory=uuid4)
    name: str
    owner_user_id: UUID
    type: GraphType
    created_at: datetime = Field(default_factory=utcnow)


class GraphMembership(ORMBase):
    """PRD GRAPH-007, RAUTH-001 (dimension 1: membership).

    NOTE (01 §3.2 Validation [LOCKED]): membership is *necessary but not
    sufficient* to read any specific resource — that also needs the
    visibility check (Rule V1). This schema does not, and must not, expose
    any method implying membership alone grants read access.
    """

    membership_id: UUID = Field(default_factory=uuid4)
    graph_id: UUID
    user_id: UUID
    role: MembershipRole
    granted_by: UUID
    granted_at: datetime = Field(default_factory=utcnow)
    revoked_at: datetime | None = None
