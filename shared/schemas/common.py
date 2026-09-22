"""Cross-cutting field families used by many entities.

Source: 01_DATA_MODEL_SCHEMA.md §1 ("Two cross-cutting field families appear
on many entities and are defined once (§1) then referenced").

IMPORTANT — read before touching this file:
These mixins represent *fields only*. They intentionally carry no methods
that decide readability/authorization (no `is_readable_by()`, no
`can_access()`). PRD RAUTH-004's read predicate —
    readable(user, resource) := (resource.visibility == graph AND
        active_member(user, resource.graph_id)) OR resource.owner_user_id == user
— is a later branch's job (04_AUTHORIZATION_GRAPH_RESOURCE.md /
security-core), not foundation's. Adding that predicate here, even as a
"convenience" helper, would be exactly the kind of authorization-logic
creep the foundation task explicitly forbids.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from shared.schemas.enums import Visibility


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ORMBase(BaseModel):
    """Base for all API-facing schemas: strict, immutable-by-default,
    ORM-instance-constructible."""

    model_config = ConfigDict(
        from_attributes=True,
        extra="forbid",
        use_enum_values=False,
    )


class ProvenanceLite(ORMBase):
    """`created_at` (+ `created_by` where relevant) — audit/trace basics
    (01 §1)."""

    created_at: datetime = Field(default_factory=utcnow)


class VisibilityTriplet(ORMBase):
    """RAUTH-003/004/005 — the private-vs-graph-shared visibility model
    (01 §1.1).

    Rule V1 [LOCKED]: presence of a `graph_id` on a resource never implies
    readability by itself — `graph_id` is a *scoping* field the entity that
    embeds this mixin must declare separately (it is not part of the
    triplet itself, per 01 §1.1's own framing: "Any entity carrying
    `graph_id` + user content MUST carry the visibility triplet; `graph_id`
    alone is not an authorization field.").

    Rule V2 [LOCKED]: changing `visibility` private -> graph is owner-only
    and audited (AuditEvent) — never a side effect. Foundation does not
    implement that mutation path; it only represents the field.
    """

    visibility: Visibility = Visibility.PRIVATE
    owner_user_id: UUID
    source_user_id: UUID
