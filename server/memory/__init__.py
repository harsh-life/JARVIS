"""Memory / context — 11_MEMORY_CONTEXT_VISIBILITY.md.

This branch implements the **authorized hydration boundary** the runtime calls
(11 §3, GRAPH-004), not the Mem0 store itself. Mem0 lives in its own vector
store (`hypermind_memories`, STORE-001b) and is `11`'s branch; it must never be
folded into the relational database (VAULT-003/MEM-001), so this package defines
the `MemoryStore` port that branch implements and applies the visibility rule
around it.

Until a store is configured, hydration degrades explicitly — FAIL-008,
"long-term memory temporarily unavailable" — and the task proceeds on session
context. That is `P3`: absent, not erroring.
"""

from server.memory.hydration import (
    AuthorizedContextHydrator,
    HydratedContext,
    MemoryCandidate,
    MemoryStore,
)

__all__ = ["AuthorizedContextHydrator", "HydratedContext", "MemoryCandidate", "MemoryStore"]
