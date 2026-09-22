"""Memory hydration for the agent runtime — 11_MEMORY_CONTEXT_VISIBILITY.md §3.

**Scope boundary, stated plainly (matches the pattern every foundation/
security-core placeholder already used for its own subsystem):** the Mem0
store, its ChromaDB collections, and the `mem0_readable()` visibility
predicate (11 §2, RAUTH-003) do not exist in any branch yet — `docs/
OD_A1_BR_T2.md` §4 already records this ("No Mem0 integration exists in any
branch yet"). This module does not build them. It defines the interface the
agent runtime consults (`server.agent.ports.MemoryHydrator`, satisfied
structurally) and ships the only honest implementation available before `11`
lands: one that returns nothing, so context assembly degrades to "no memory"
rather than fabricating results or reaching into a store that isn't there.

When `11` is implemented, it replaces `NullMemoryHydrator` with a real one
that applies `mem0_readable()` *inside* the retrieval query (11 §2's
`[REC]` — filtered in the query, not fetched-then-filtered) and is
constructed the same way (closed over the calling principal), so
`server/gateway/runtime.py`'s wiring changes in exactly one place.
"""

from __future__ import annotations

from shared.schemas.runtime import MemoryItem


class NullMemoryHydrator:
    """Structurally satisfies `server.agent.ports.MemoryHydrator`.

    Returning an empty list is the fail-closed choice for "there is no memory
    subsystem yet" — never a fabricated or cached-elsewhere result, and never
    an error (05 §6's "dependency down -> degrade with explicit note": memory
    being entirely absent degrades to no memory, it does not fail the task).
    """

    async def hydrate(self, query: str, *, limit: int) -> list[MemoryItem]:
        return []


__all__ = ["NullMemoryHydrator"]
