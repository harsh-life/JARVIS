"""The native runtime provider (docs/29 §7.4) — the existing JARVIS task
runtime, used as an `AgentRuntimeProvider`.

It invents no executor. A run is an ordinary task of the present user: the
composition root binds a `NativeRuntimePort` to the request that asked for the
run — its authenticated principal, its store transaction, the runtime — and
this provider only hands that port the `AgentRunContext`. It never sees a
principal, a session, a device, a secret or a key, and it decides nothing:
every model call and tool effect of the run goes through the runtime's
existing path (the envelope gate, activation, the engine, confirmation).

* `provision` is a no-op: a native agent is only its JARVIS definition.
* `deprovision` purges what JARVIS keeps for the agent at runtime (notebook,
  inbox), so no runtime-side state survives it.
* `list_runtime_agents` is empty: the native runtime holds no agent of its own
  outside JARVIS's tables, so there is never an orphan to reconcile.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from server.agents.registry.runtimes import NATIVE_RUNTIME, NATIVE_RUNTIME_ID
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


class NativeRuntimePort(Protocol):
    async def start(self, ctx: AgentRunContext) -> AgentRunStatus: ...

    async def cancel(self, run_id: uuid.UUID, reason: CancelReason) -> None: ...

    async def status_of(self, run_id: uuid.UUID) -> AgentRunStatus: ...

    async def purge(self, agent_id: uuid.UUID) -> None: ...

    async def export(self, agent_id: uuid.UUID) -> bytes: ...


def _native(runtime_id: str) -> None:
    if runtime_id != NATIVE_RUNTIME_ID:
        raise ValueError(f"not a native runtime handle: {runtime_id!r}")


class NativeRuntimeProvider:
    """Satisfies `server.agents.providers.base.AgentRuntimeProvider`."""

    def __init__(self, port: NativeRuntimePort) -> None:
        self._port = port
        self.profile: AgentRuntimeProfile = NATIVE_RUNTIME.model_copy(update={"enabled": True})

    async def health(self) -> ProviderHealth:
        return ProviderHealth(ok=True, detail="in-process")

    async def provision(self, spec: CompiledAgentSpec) -> RuntimeRef:
        return RuntimeRef(runtime_id=NATIVE_RUNTIME_ID, agent_id=spec.agent_id, version=spec.version)

    async def start_run(self, ctx: AgentRunContext) -> RunHandle:
        await self._port.start(ctx)
        return RunHandle(runtime_id=NATIVE_RUNTIME_ID, run_id=ctx.run_id)

    async def cancel_run(self, handle: RunHandle, reason: CancelReason) -> None:
        _native(handle.runtime_id)
        await self._port.cancel(handle.run_id, reason)

    async def run_status(self, handle: RunHandle) -> AgentRunStatus:
        _native(handle.runtime_id)
        return await self._port.status_of(handle.run_id)

    async def deprovision(self, ref: RuntimeRef) -> DeprovisionReceipt:
        _native(ref.runtime_id)
        await self._port.purge(ref.agent_id)
        return DeprovisionReceipt(ref=ref, removed=True)

    async def export_state(self, ref: RuntimeRef) -> bytes | None:
        _native(ref.runtime_id)
        return await self._port.export(ref.agent_id)

    async def list_runtime_agents(self) -> list[RuntimeRef]:
        return []


__all__ = ["NativeRuntimePort", "NativeRuntimeProvider"]
