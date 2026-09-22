"""The tool registry — 07 §1: "no contract → not registerable → cannot run."

`[LOCKED]` (TOOL-003): a tool must publish a `ToolContract` before it is
callable, and an `enabled` `ToolConfiguration` is what turns a registered
contract into a usable tool in a given scope (07 §1). This module is the
in-process bookkeeping for both — not a database table, because no branch
before this one has decided tools need durable storage (`01` §10 recommends a
git-backed registry, which the Execution branch owns) and the registry's
lifetime already matches the process's (it is rebuilt from `AppConfig` at
startup by `server/gateway/runtime.py`, the composition root).

This module does not decide *whether* an operation is permitted (that's `04`
D5 + `07`'s risk-tier policy, both in `server/graph`/`server/capabilities`,
which this package must not import — see `server/agent/ports.py`'s module
docstring for the same reasoning applied one layer up) — it only tracks
*which* tools exist and are enabled, and dispatches to the one an already-
authorized call names.
"""

from __future__ import annotations

from dataclasses import dataclass

from server.tools.executor import ToolExecutor
from shared.schemas.agent_config import ToolConfiguration, ToolContract
from shared.schemas.runtime import ToolExecutionFailed, ToolInvocationRequest, ToolResult


@dataclass(frozen=True)
class RegisteredTool:
    contract: ToolContract
    configuration: ToolConfiguration
    executor: ToolExecutor


class ToolRegistry:
    """In-process, built once at startup from `AppConfig.tools` (+ any
    model-tool contracts `server/modeltools` synthesizes) and read many times
    per request. Not thread-safe beyond what the GIL already gives a
    single-process asyncio app — registration happens only at startup, never
    concurrently with a request.
    """

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(
        self, contract: ToolContract, configuration: ToolConfiguration, executor: ToolExecutor
    ) -> None:
        """TOOL-001/003: a contract is required; registering a second
        contract for the same `tool_id` replaces the first (a startup-time
        config reload, never a mid-request mutation)."""

        if contract.tool_id != configuration.tool_id:
            raise ValueError(
                f"contract.tool_id {contract.tool_id!r} does not match "
                f"configuration.tool_id {configuration.tool_id!r}"
            )
        self._tools[contract.tool_id] = RegisteredTool(
            contract=contract, configuration=configuration, executor=executor
        )

    def get(self, tool_id: str) -> RegisteredTool | None:
        return self._tools.get(tool_id)

    def is_enabled(self, tool_id: str) -> bool:
        tool = self._tools.get(tool_id)
        return tool is not None and tool.configuration.enabled

    def list_enabled_contracts(self) -> list[ToolContract]:
        """07 §1's discovery surface — a *registered-but-not-enabled* tool
        (07 §1: "inert... until enabled by a human") is never listed, so the
        agent never learns a provisional tool exists to try invoking it."""

        return [t.contract for t in self._tools.values() if t.configuration.enabled]

    async def dispatch(self, request: ToolInvocationRequest) -> ToolResult:
        """The single dispatch entry point (07 §8 `EXEC`).

        `[LOCKED]` TOOL-003/07 §1: unregistered or disabled is a hard failure
        here, never a silent no-op — the caller (an already-authorized
        `AccessRequest` reached D5 only because *some* capability matched;
        this is the second, independent gate that the concrete tool actually
        exists and is turned on).
        """

        tool = self._tools.get(request.tool_id)
        if tool is None:
            raise ToolExecutionFailed(f"tool {request.tool_id!r} is not registered")
        if not tool.configuration.enabled:
            raise ToolExecutionFailed(f"tool {request.tool_id!r} is not enabled")
        return await tool.executor.execute(request)


__all__ = ["RegisteredTool", "ToolRegistry"]
