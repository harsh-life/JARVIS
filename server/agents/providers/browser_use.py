"""The Browser Use runtime provider (docs/29 §7.3, §21.1 P2; OD-AF-6).

`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]` (docs/29). Browser Use is
**untrusted execution infrastructure**: this provider turns one
`AgentRunContext` into one disposable container, through a port the
composition root satisfies (the per-run workspace, sockets and engine), and
does nothing else. Like every provider (AF-C3) it cannot reach the engine,
grants, the SecretStore or the gateway's HTTP layer, and it decides nothing.

What it hands the container is taken from the context and nothing else:

* the **model** run token (`model_run_token`) — the only credential in the
  container (docs/29 §21 item 3), useless outside this run and the Model
  Gateway, revocable;
* the in-container paths of the run's two sockets;
* the run's assembled input, as the task.

It never passes the context's **tool** token: a P2 runtime's effects stay
inside its disposable workspace and its results come back as inbox data, so
it makes no Tool Gateway call (OD-TOOL-3: no MCP; register §2L). There is no
field for a principal, a session, a device, a secret or a provider key to
pass — `AgentRunContext` has none.

* `provision` is a no-op: nothing persists between runs (a fresh profile,
  no stored credentials);
* `cancel_run` is the deterministic kill path, safe to repeat;
* `deprovision` has nothing to remove — the workspace dies with the run;
* `list_runtime_agents` is what the engine reports, for reconciliation.
"""

from __future__ import annotations

import uuid
from typing import Mapping, Protocol

from server.agents.registry.runtimes import BROWSER_USE_RUNTIME, BROWSER_USE_RUNTIME_ID
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

CONTAINER_MODEL_SOCKET = "/run/jarvis/model.sock"
CONTAINER_EGRESS_SOCKET = "/run/jarvis/egress.sock"


class ContainerPort(Protocol):
    async def launch(self, *, run_id: uuid.UUID, agent_id: uuid.UUID, env: Mapping[str, str],
                     task_input: str) -> str: ...
    async def kill(self, run_id: uuid.UUID, reason: str) -> None: ...
    async def status_of(self, run_id: uuid.UUID) -> AgentRunStatus: ...
    async def running(self) -> list[uuid.UUID]: ...
    async def healthy(self) -> bool: ...


def _browser(runtime_id: str) -> None:
    if runtime_id != BROWSER_USE_RUNTIME_ID:
        raise ValueError(f"not a Browser Use handle: {runtime_id!r}")


def container_env(ctx: AgentRunContext) -> dict[str, str]:
    """The container's whole environment, from the context alone."""

    if not ctx.model_run_token:
        raise ValueError("a Browser Use run needs its model token")
    return {
        "JARVIS_RUN_TOKEN": ctx.model_run_token,
        "JARVIS_MODEL_SOCKET": CONTAINER_MODEL_SOCKET,
        "JARVIS_EGRESS_SOCKET": CONTAINER_EGRESS_SOCKET,
    }


class BrowserUseRuntimeProvider:
    """Satisfies `server.agents.providers.base.AgentRuntimeProvider`."""

    def __init__(self, port: ContainerPort) -> None:
        self._port = port
        self.profile: AgentRuntimeProfile = BROWSER_USE_RUNTIME.model_copy(update={"enabled": True})

    async def health(self) -> ProviderHealth:
        ok = await self._port.healthy()
        return ProviderHealth(ok=ok, detail="" if ok else "container engine unavailable")

    async def provision(self, spec: CompiledAgentSpec) -> RuntimeRef:
        return RuntimeRef(runtime_id=BROWSER_USE_RUNTIME_ID, agent_id=spec.agent_id, version=spec.version)

    async def start_run(self, ctx: AgentRunContext) -> RunHandle:
        name = await self._port.launch(run_id=ctx.run_id, agent_id=ctx.agent_id, env=container_env(ctx),
                                       task_input=ctx.input_text)
        return RunHandle(runtime_id=BROWSER_USE_RUNTIME_ID, run_id=ctx.run_id, external_ref=name)

    async def cancel_run(self, handle: RunHandle, reason: CancelReason) -> None:
        _browser(handle.runtime_id)
        await self._port.kill(handle.run_id, reason.value)

    async def run_status(self, handle: RunHandle) -> AgentRunStatus:
        _browser(handle.runtime_id)
        return await self._port.status_of(handle.run_id)

    async def deprovision(self, ref: RuntimeRef) -> DeprovisionReceipt:
        _browser(ref.runtime_id)
        return DeprovisionReceipt(ref=ref, removed=True, detail="no runtime-side state: each run is disposable")

    async def export_state(self, ref: RuntimeRef) -> bytes | None:
        _browser(ref.runtime_id)
        return None

    async def list_runtime_agents(self) -> list[RuntimeRef]:
        # A P2 runtime holds no agent between runs; live containers are the
        # reconciler's (they are runs, not agents).
        return []


__all__ = ["BrowserUseRuntimeProvider", "ContainerPort", "container_env"]
