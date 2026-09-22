"""Secret entity (handle only) — SecretReference.

Source: 01_DATA_MODEL_SCHEMA.md §8. PRD SECRET-001/002, SUPER-001.

CRITICAL invariant this file exists to preserve: **no field on this model,
or on any model in this codebase, ever holds a secret value.** SecretReference
is metadata about a secret (a handle + its class + its owner scope +
timestamps) — the value itself lives only inside the SecretStore
(12_SECRETSTORE.md), which this branch explicitly does not implement.

There is deliberately no `get_value()`, `resolve()`, or similar function
anywhere near this schema. Foundation must not create a raw-secret getter
"for convenience" (§15 of this branch's instructions) — that absence is
itself the security property (P2, "absence over restriction").
"""

from __future__ import annotations

from datetime import datetime

from pydantic import ConfigDict, Field

from shared.schemas.common import ORMBase, utcnow
from shared.schemas.enums import SecretClass, SecretOwnerScopeType


class SecretReference(ORMBase):
    """PRD SECRET-001/002, SUPER-001 (01 §8.1).

    `secret_ref` is the PK — an opaque handle, not a lookup key into secret
    material stored anywhere in this table. `class_ = SecretClass.MASTER_KEY`
    references are never resolvable by the agent or user tools (SUPER-001) —
    that resolution rule lives in the SecretStore, not here.
    """

    secret_ref: str
    owner_scope_type: SecretOwnerScopeType
    owner_scope_id: str | None = None
    class_: SecretClass = Field(alias="class")
    created_at: datetime = Field(default_factory=utcnow)
    rotated_at: datetime | None = None
    revoked_at: datetime | None = None

    model_config = ConfigDict(
        from_attributes=True,
        extra="forbid",
        populate_by_name=True,
    )
