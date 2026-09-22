"""The tool-execution boundary — 07_TOOL_CAPABILITY_EXECUTION.md §1/§8.

`[LOCKED]` scope note: this branch (runtime) owns the *registry and dispatch*
half of `07` — enough to unblock `05`'s loop — not the concrete execution
primitives. "DO NOT implement the actual platform execution systems" (this
branch's own instructions): Android/Shizuku, the filesystem sandbox, and
network egress enforcement belong to the Execution branch (`08`/`09`/`10`).
Nothing in this package executes a filesystem write, a network call, or a
device action; `ToolExecutor` is the interface those branches implement.

Deliberately does **not** import `server.agent`: it and `server.agent` are
independent siblings under the layering contract (both occupy the
`agent | modeltools | models | tools | memory | vault | scheduler | voice`
band), so `ToolExecutor` is declared here, using only `shared.schemas`
vocabulary, and satisfied structurally — the same "no inheritance, no cross-
import" pattern `server/capabilities/grants.py` already uses for
`server.graph.ports.CapabilityChecker`.
"""

from __future__ import annotations

from typing import Protocol

from shared.schemas.runtime import ToolInvocationRequest, ToolResult


class ToolExecutor(Protocol):
    """What a concrete tool (or a model-tool, `server/modeltools`) implements.

    Called only after `04` has already authorized the call (07 §8's `EXEC`
    node) — an executor has no access to the `AccessRequest` or the
    principal's identity beyond what `ToolInvocationRequest` explicitly
    carries, so it structurally cannot make its own authorization decision.
    """

    async def execute(self, request: ToolInvocationRequest) -> ToolResult: ...


__all__ = ["ToolExecutor"]
