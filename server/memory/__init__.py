"""Memory / context / visibility — 11_MEMORY_CONTEXT_VISIBILITY.md.

Still not implemented by this branch beyond the hydration *interface* the
agent runtime needs to exist (`server/memory/hydrator.py`) — see that
module's docstring for the explicit scope boundary. `server.memory` remains
forbidden from importing `server.secrets` (pyproject's "Memory/vault never
resolve secrets", GRAPH-009), which this package continues to honor: nothing
here needs a secret, now or once `11` is implemented.

Owning subsystem doc: 11_MEMORY_CONTEXT_VISIBILITY.md
"""

from server.memory.hydrator import NullMemoryHydrator

__all__ = ["NullMemoryHydrator"]
