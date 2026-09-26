"""Authorized, relevance-bounded context hydration (11 §2/§3, MEM-002, RAUTH-003).

The retrieval chain this module is:

    authenticated principal            (server-derived, never from the request body)
        ↓
    authorization / visibility         (pushed INTO the store query, 11 §2 [REC])
        ↓
    re-check with RAUTH-004            (the engine's own `readable()` — one predicate)
        ↓
    relevance bound                    (top-K, character budget — never a history dump)
        ↓
    model context

Two properties it guarantees regardless of how a `MemoryStore` behaves:

* **Another user's private fact never reaches the model.** The store is asked
  only for facts the principal owns or graph-visible facts in the task's graph
  where the principal is an active member. Whatever the store returns is then
  re-checked with `readable()`; a store that ignored the filter would have its
  extra rows dropped here, not passed on (defense in depth for MEM-T1).
* **The model never browses memory.** It receives the hydrated text items only
  — no store handle, no query interface, no ids of facts it was not given. There
  is no tool for "list all memories".

Same-user continuity is intentional: facts are keyed on the *user*, not the
device or session, so every device of one user hydrates the same authorized
state.

Minimization choice (`[PROPOSED]`, OD-MEM-1): graph-visible facts are hydrated
only from the task's own graph, even though RAUTH-004 would let a member read
graph-visible facts in their other graphs too. A task in graph X therefore never
carries graph Y's shared context into output that may be shared back into X.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from server.graph.ports import ResourceDescriptor
from server.graph.predicate import readable
from server.memory.provider import MemoryCandidate, MemoryStore
from shared.schemas.authorization import Principal, ResourceType

logger = logging.getLogger("hypermind.memory.hydration")

FAIL_008_NOTE = "long-term memory temporarily unavailable; proceeding on session context (FAIL-008)"


# Returns the graphs in which the user is an *active* member, read live.
ActiveGraphLister = Callable[[AsyncSession, uuid.UUID], Awaitable[set[uuid.UUID]]]


@dataclass
class HydratedContext:
    items: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    dropped_unreadable: int = 0


class AuthorizedContextHydrator:
    def __init__(
        self,
        *,
        store: MemoryStore | None,
        active_graph_ids: ActiveGraphLister,
        top_k: int,
        max_chars: int = 4000,
    ) -> None:
        self._store = store
        self._active_graph_ids = active_graph_ids
        self._top_k = max(0, top_k)
        self._max_chars = max_chars

    async def hydrate(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        graph_id: uuid.UUID | None,
        query: str,
    ) -> HydratedContext:
        if self._store is None:
            return HydratedContext(notes=[FAIL_008_NOTE])
        if self._top_k == 0:
            return HydratedContext()

        try:
            member_of = await self._active_graph_ids(session, principal.user_id)
            task_graphs = frozenset({graph_id} & member_of) if graph_id is not None else frozenset()
            candidates = await self._store.search(
                query=query,
                owner_user_id=principal.user_id,
                readable_graph_ids=task_graphs,
                limit=self._top_k,
            )
        except Exception:  # noqa: BLE001 — a non-security dependency degrades (FAIL-008)
            logger.exception("memory store unavailable; degrading")
            return HydratedContext(notes=[FAIL_008_NOTE])

        result = HydratedContext()
        accepted: list[MemoryCandidate] = []
        for candidate in candidates:
            descriptor = ResourceDescriptor(
                resource_type=ResourceType.MEM0FACT,
                resource_ref=str(candidate.fact_id),
                owner_user_id=candidate.owner_user_id,
                visibility=candidate.visibility,
                graph_id=candidate.graph_id,
            )
            in_task_graph = candidate.graph_id is not None and candidate.graph_id in task_graphs
            if not readable(
                user_id=principal.user_id,
                resource=descriptor,
                is_active_member_of_resource_graph=in_task_graph,
            ):
                # A store that returned this ignored the filter. Dropped, never
                # passed on — and counted so the leak is visible to operators.
                result.dropped_unreadable += 1
                continue
            accepted.append(candidate)

        if result.dropped_unreadable:
            logger.error(
                "memory store returned %d fact(s) the principal may not read; dropped",
                result.dropped_unreadable,
            )

        accepted.sort(key=lambda c: c.score, reverse=True)
        budget = self._max_chars
        for candidate in accepted[: self._top_k]:
            text = candidate.content.strip()
            if not text or len(text) > budget:
                continue
            result.items.append(text)
            budget -= len(text)
        return result
