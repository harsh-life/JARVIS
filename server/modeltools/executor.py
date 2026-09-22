"""LLM-as-a-Tool — 06_MODEL_PROVIDER_LLM_TOOL.md §3.

`[LOCKED]` (06 §3): "A model-tool... flows through the *same* tool machinery
as any other tool (07): registered contract, required capability,
authorization via 04, bounded execution, metering." This module is that
machinery's tool-family member — a `ToolExecutor` (`server.tools.executor`)
backed by a `ModelProvider` call instead of a filesystem/network/device
primitive. It is registered into the *same* `server.tools.ToolRegistry` as
any other tool by the gateway composition root, under the model-tool's own
`tool_id` — there is no separate "model-tool registry."

`[LOCKED]` (MODELTOOL-003): "No MCP-per-provider... the internal model-tool
abstraction is native." This executor calls `server.models`' normalized
`ModelProvider` interface directly — no MCP indirection for an internal
model-tool.
"""

from __future__ import annotations

from typing import Protocol

from shared.schemas.runtime import (
    GenerationPolicy,
    ModelMessage,
    ModelResult,
    ModelUnavailable,
    ToolInvocationRequest,
    ToolResult,
)


class _ModelProvider(Protocol):
    """The one method this executor needs from `server.models.provider.
    ModelProvider`, restated structurally rather than imported — `modeltools`
    and `models` are independent siblings under the layering contract, so
    neither may import the other (mirrors `server/tools/executor.py`'s
    identical reasoning for not importing `server.agent`)."""

    async def invoke(
        self, messages: list[ModelMessage], policy: GenerationPolicy, timeout: float
    ) -> ModelResult: ...


class ModelToolExecutor:
    """Structurally satisfies `server.tools.executor.ToolExecutor`.

    One instance per configured model-tool entry (`AppConfig.models_as_tools`,
    `01` §9.3), each wrapping its own `ModelProvider` — provider isolation
    (MODELTOOL-004) falls out of this for free: a failing model-tool's
    provider is a distinct object from any other tool's, so one adapter's
    outage cannot touch another's state.
    """

    def __init__(self, *, tool_id: str, provider: _ModelProvider) -> None:
        self._tool_id = tool_id
        self._provider = provider

    async def execute(self, request: ToolInvocationRequest) -> ToolResult:
        prompt = str(request.arguments.get("prompt", ""))
        if not prompt:
            return ToolResult(
                tool_id=self._tool_id, success=False, error="model-tool call had no prompt"
            )

        messages = [ModelMessage(role="user", content=prompt)]
        try:
            result = await self._provider.invoke(messages, GenerationPolicy(), 30.0)
        except ModelUnavailable as exc:
            # 06 §4: "a failing/misbehaving adapter fails *that call only*" —
            # surfaced as an ordinary `ToolResult` failure (05 §6's normal
            # "tool failure -> observation" path), not a crash of the
            # orchestrator or of any other tool.
            return ToolResult(tool_id=self._tool_id, success=False, error=str(exc))

        return ToolResult(
            tool_id=self._tool_id,
            success=True,
            # 06 §4 [LOCKED]: a model-tool's output is untrusted content
            # flowing back into the agent's context — this method returns it
            # as a plain `ToolResult.output` string, exactly like any other
            # tool's observation; `04`/`07` still gate whatever the agent
            # proposes *next* on the strength of it (nothing here elevates it
            # to a trusted fact).
            output=result.content,
            tokens_or_units=result.tokens_used,
        )


__all__ = ["ModelToolExecutor"]
