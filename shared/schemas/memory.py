"""Memory & knowledge entities — Mem0Fact, VaultDocument, VaultQuery.

Source: 01_DATA_MODEL_SCHEMA.md §5.

Storage note (STORE-001, VAULT-003): Mem0Fact lives in the Mem0 vector
store + SQLite (`hypermind_memories` collection); VaultDocument lives in a
Git-backed markdown tree indexed into a *distinct* ChromaDB collection
(`hypermind_vault`). Foundation defines the **data shapes only** — it does
not stand up Mem0, ChromaDB, or any embedding pipeline (that is
11_MEMORY_CONTEXT_VISIBILITY.md's job, and explicitly listed in this
branch's instructions as a later "memory backend" concern). No table for
either entity is created in the relational store here.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from shared.schemas.common import ORMBase, utcnow
from shared.schemas.enums import FactType, Visibility


class Mem0Fact(ORMBase):
    """PRD MEM-001..006, RAUTH-003, EMO-002, LORA-002 (01 §5.1).

    `fact_type` restricted to the enum is the *structural* guard against
    emotional/relationship content (EMO-002) — DM-T3: a fact_type outside
    the enum is rejected, not coerced. Pydantic's own enum validation gives
    us this for free; there is no separate content-moderation logic here
    (and none should be added in foundation).
    """

    fact_id: UUID = Field(default_factory=uuid4)
    owner_user_id: UUID
    source_user_id: UUID
    graph_id: UUID
    visibility: Visibility = Visibility.PRIVATE
    fact_type: FactType
    content: str
    timestamp: datetime = Field(default_factory=utcnow)
    source_session_id: UUID | None = None
    embedding_ref: str | None = None

    @field_validator("content")
    @classmethod
    def _content_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Mem0Fact.content must not be empty")
        return v


class VaultDocument(ORMBase):
    """PRD VAULT-001..005 (01 §5.2) — indexing side.

    Carries no visibility triplet: vault content is shared-curated by
    construction (VAULT-004), never per-user.
    """

    doc_id: UUID = Field(default_factory=uuid4)
    source_file: str
    domain: str
    chunk: str
    embedding_ref: str | None = None
    curated_by: UUID | None = None


class VaultQueryRequest(ORMBase):
    domain: str
    question: str
    top_k: int = Field(default=5, gt=0, le=50)


class VaultQueryResultItem(ORMBase):
    chunk: str
    source_file: str
    relevance_score: float = Field(ge=0.0, le=1.0)


class VaultQueryResponse(ORMBase):
    results: list[VaultQueryResultItem] = Field(default_factory=list)


# ── 02 §7 memory endpoint bodies ─────────────────────────────────────────────


class MemoryCreateRequest(ORMBase):
    """`POST /api/v1/memory`. No `visibility`, owner or source field: a new fact
    is the caller's and `private` (RAUTH-005, PHONE-003). `graph_id` is a claim
    the engine checks (D1); omitted, the session's active graph is used."""

    fact_type: FactType
    content: str = Field(min_length=1, max_length=4000)
    graph_id: UUID | None = None


class MemoryPatchRequest(ORMBase):
    """`PATCH /api/v1/memory/{fact_id}` — owner-only correction and/or share."""

    content: str | None = Field(default=None, min_length=1, max_length=4000)
    visibility: Visibility | None = None

    @model_validator(mode="after")
    def _not_both_empty(self) -> "MemoryPatchRequest":
        if self.content is None and self.visibility is None:
            raise ValueError("at least one of content or visibility is required")
        return self


class MemoryListResponse(ORMBase):
    items: list[Mem0Fact] = Field(default_factory=list)
    next_cursor: str | None = None
