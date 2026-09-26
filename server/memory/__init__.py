"""Memory / context — 11_MEMORY_CONTEXT_VISIBILITY.md, docs/21_MEMORY_PROVIDER_VAULT.md.

* `provider` — the `MemoryProvider` contract everything else depends on.
* `mem0_provider` — the self-hosted Mem0 OSS adapter (the only module that
  imports `mem0`), opened by the composition root when `memory.enabled`.
* `gate` — the deterministic write gate every stored fact passes.
* `extraction` — prompt and strict parser for runtime-owned extraction; the
  model call itself is the runtime's, metered like every other.
* `hydration` — the authorized hydration boundary: visibility pushed into the
  store query, every result re-checked with the engine's `readable()`, then a
  relevance and size bound.

Authorization is never decided here: the engine decides, the provider stores.
Until a store is configured, hydration degrades explicitly — FAIL-008,
"long-term memory temporarily unavailable" — and the task proceeds on session
context (`P3`: absent, not erroring).

`mem0_provider` is deliberately not imported here, so importing this package
never imports Mem0.
"""

from server.memory.gate import GateResult, MemoryWriteGate
from server.memory.hydration import AuthorizedContextHydrator, HydratedContext
from server.memory.provider import (
    MemoryCandidate,
    MemoryProvider,
    MemoryProviderUnavailable,
    MemoryStore,
)

__all__ = [
    "AuthorizedContextHydrator",
    "GateResult",
    "HydratedContext",
    "MemoryCandidate",
    "MemoryProvider",
    "MemoryProviderUnavailable",
    "MemoryStore",
    "MemoryWriteGate",
]
