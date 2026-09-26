"""The MemoryProvider contract (docs/21 §1).

JARVIS depends on this Protocol, never on Mem0: replacing Mem0 later is a new
adapter plus configuration, with no change to authorization, the runtime, or the
API (`[OWNER-RATIFIED]`).

What a provider is — and is not:

* It **stores and retrieves**. It never decides whether a principal may read or
  change a fact; the authorization engine does (docs/21 §0 rule 1). `search`
  applies the visibility predicate inside its own query as defence in depth,
  and the hydrator re-checks every result with the engine's `readable()`.
* Every method other than `search` is called only *after* the engine has
  authorized that exact operation. There is deliberately no "search across
  users" and no "list everything" method: no JARVIS code path can ask a
  provider for another user's private facts.
* It stores only what the deterministic write gate (`server/memory/gate.py`)
  admitted. The provider never admits content itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol, Sequence

from shared.schemas.enums import Visibility
from shared.schemas.memory import Mem0Fact


class MemoryProviderUnavailable(Exception):
    """The store could not complete the operation (FAIL-008). The message names
    the operation only — never fact content, ids of other users' facts, or a
    backend error string."""


@dataclass(frozen=True)
class MemoryCandidate:
    """One search result, reduced to what hydration and the visibility re-check
    need. No provider-native record, id or metadata travels further."""

    fact_id: str
    content: str
    owner_user_id: uuid.UUID
    visibility: Visibility
    graph_id: uuid.UUID | None
    score: float = 0.0


class MemoryStore(Protocol):
    """The read half hydration needs (11 §2). `[LOCKED]` the visibility filter is
    applied **inside** the query: return only facts with
    `owner_user_id == owner_user_id`, or with `visibility == graph` and
    `graph_id in readable_graph_ids`."""

    async def search(
        self,
        *,
        query: str,
        owner_user_id: uuid.UUID,
        readable_graph_ids: frozenset[uuid.UUID],
        limit: int,
    ) -> Sequence[MemoryCandidate]: ...


class MemoryProvider(MemoryStore, Protocol):
    """docs/21 §1 — the full provider contract."""

    async def add(self, fact: Mem0Fact) -> uuid.UUID:
        """Store an already gated and authorized fact. Always stored `private`
        whatever `fact.visibility` says: sharing is a separate, owner-only,
        audited act (`set_visibility`, RAUTH-005). Returns the fact id — an
        existing one when the same owner already holds the same fact (docs/21 §4
        step 5), so a duplicate never becomes a second record."""
        ...

    async def get(self, fact_id: uuid.UUID) -> Mem0Fact | None:
        """The fact, or `None`. The caller authorizes the result via the engine."""
        ...

    async def list_facts(
        self,
        *,
        owner_user_id: uuid.UUID,
        readable_graph_ids: frozenset[uuid.UUID],
        limit: int,
    ) -> Sequence[Mem0Fact]:
        """Recall for the owner-facing `GET /memory` (02 §7), filtered in-query
        exactly like `search`."""
        ...

    async def update_content(self, fact_id: uuid.UUID, content: str) -> None:
        """Owner-only correction (D3), already gated and authorized."""
        ...

    async def set_visibility(self, fact_id: uuid.UUID, visibility: Visibility) -> None:
        """Owner-only share/unshare (RAUTH V2), already authorized; audited by the caller."""
        ...

    async def delete(self, fact_id: uuid.UUID) -> bool:
        """Remove the fact and every copy of it. `False` if it did not exist."""
        ...

    async def delete_all_for_user(self, user_id: uuid.UUID) -> int:
        """Account deletion (LIFE-003): every fact the user owns, private or shared."""
        ...

    async def delete_graph_shared(self, graph_id: uuid.UUID) -> int:
        """Graph deletion: graph-shared facts of that graph only — never a
        member's private facts formed there (11 §5, MEM-T9)."""
        ...

    async def health(self) -> bool: ...


__all__ = ["MemoryCandidate", "MemoryProvider", "MemoryProviderUnavailable", "MemoryStore"]
