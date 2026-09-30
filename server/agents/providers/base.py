"""The AgentRuntimeProvider boundary (docs/29 §7.3) — **declared, not wired**.

A provider turns a compiled spec into runs. It is an engine:

* it receives only a `CompiledAgentSpec` and an `AgentRunContext` — never a
  `Principal`, a session token, a device credential, a SecretStore handle or a
  provider key;
* it never decides whether an operation is allowed: every effect goes back
  through JARVIS — the native runtime's existing authorization path, or (for a
  future external runtime) the Agent Gateway, where the envelope gate, 04, the
  tiers and confirmation apply exactly as for any task (docs/29 §13);
* `provision` is idempotent on (agent_id, version); `cancel_run` is safe to
  call repeatedly; `deprovision` leaves `list_runtime_agents()` without the
  ref (AGENT-T19).

Phase 2 implements the native provider (`native.py`), which starts an
ordinary task through a runtime port the composition root satisfies.
"""

from __future__ import annotations

from typing import Protocol

from shared.schemas.agent_factory import (
    AgentRunContext,
    AgentRunStatus,
    AgentRuntimeProfile,
    CancelReason,
    CompiledAgentSpec,
    DeprovisionReceipt,
    ProviderHealth,
    RunHandle,
    RuntimeRef,
)


class AgentRuntimeProvider(Protocol):
    profile: AgentRuntimeProfile

    async def health(self) -> ProviderHealth: ...

    async def provision(self, spec: CompiledAgentSpec) -> RuntimeRef: ...

    async def start_run(self, ctx: AgentRunContext) -> RunHandle: ...

    async def cancel_run(self, handle: RunHandle, reason: CancelReason) -> None: ...

    async def run_status(self, handle: RunHandle) -> AgentRunStatus: ...

    async def deprovision(self, ref: RuntimeRef) -> DeprovisionReceipt: ...

    async def export_state(self, ref: RuntimeRef) -> bytes | None: ...

    async def list_runtime_agents(self) -> list[RuntimeRef]: ...


__all__ = ["AgentRuntimeProvider"]
