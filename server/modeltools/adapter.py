"""The model-tool adapter and its ToolContract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from server.models.provider import ChatMessage, ModelProvider, ModelUnavailable
from shared.schemas.agent import (
    ExecutionPlatform,
    OperationSpec,
    ToolInvocation,
    ToolOutput,
)
from shared.schemas.agent_config import ToolContract
from shared.schemas.authorization import Operation, ResourceType
from shared.schemas.enums import RiskCategory, UsageKind

MODEL_TOOL_CAPABILITY = "model.invoke"
MODEL_TOOL_OPERATION = "invoke"
MAX_PROMPT_CHARS = 8000

_SYSTEM = (
    "You are a specialist model invoked as a tool by another assistant. Answer the "
    "request directly. You cannot call tools and you have no authority to take actions."
)


class ModelToolAdapter:
    """Satisfies `server.tools.ToolAdapter` structurally."""

    def __init__(self, provider: ModelProvider) -> None:
        self._provider = provider

    async def execute(self, invocation: ToolInvocation) -> ToolOutput:
        prompt = invocation.arguments.get("prompt")
        spec = self._provider.spec
        if not isinstance(prompt, str) or not prompt.strip():
            return ToolOutput(
                ok=False, error="missing_prompt", usage_kind=UsageKind.MODEL_CALL, units=0,
                provider=spec.provider, model=spec.model,
            )
        prompt = prompt[:MAX_PROMPT_CHARS]
        try:
            result = await self._provider.invoke(
                [ChatMessage("system", _SYSTEM), ChatMessage("user", prompt)],
                timeout=spec.timeout_seconds,
            )
        except ModelUnavailable as exc:
            return ToolOutput(
                ok=False, error=str(exc), usage_kind=UsageKind.MODEL_CALL, units=0,
                provider=spec.provider, model=spec.model,
            )
        return ToolOutput(
            ok=True,
            content=result.content,
            usage_kind=UsageKind.MODEL_CALL,
            units=result.total_tokens,
            estimated_cost=spec.pricing.cost(
                prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens
            ),
            provider=spec.provider,
            model=spec.model,
        )


@dataclass(frozen=True)
class ModelToolParts:
    """The pieces of a `server.tools.ToolDefinition`. Returned as parts because
    `modeltools` and `tools` are independent sibling modules (16 §2); the
    composition root assembles and registers the definition."""

    contract: ToolContract
    operations: Mapping[str, OperationSpec]
    adapters: Mapping[ExecutionPlatform, ModelToolAdapter]
    is_model_tool: bool = field(default=True)
    projected_cost_per_call: float = 0.0


def model_tool_definition(tool_id: str, *, description: str, provider: ModelProvider) -> ModelToolParts:
    """Build the registrable parts for one configured model-tool.

    The contract declares the provider endpoint as its only network need; that is
    the provider-adapter egress 10 will bound. It declares no filesystem access
    and cannot send credentials — the provider key is resolved inside the
    adapter's provider, never handed to the tool as an argument.
    """

    spec = provider.spec
    contract = ToolContract(
        tool_id=tool_id,
        version="1",
        description=description or f"{spec.provider}:{spec.model} as a tool",
        input_schema={
            "type": "object",
            "properties": {"prompt": {"type": "string"}},
            "required": ["prompt"],
        },
        output_schema={"type": "string"},
        required_capability=MODEL_TOOL_CAPABILITY,
        resource_scope={"model_tool_id": tool_id},
        network={
            "required": True,
            "destinations": [spec.endpoint or f"{spec.provider}:default"],
            "internet": spec.provider != "ollama",
            "private_net": False,
            "may_send_credentials": False,
        },
        filesystem={},
        risk_category=RiskCategory.LOW_READ,
        timeout_seconds=max(1, int(spec.timeout_seconds)),
        confirmation_required=False,
        failure_behavior="observation",
        audit="every invocation",
    )
    return ModelToolParts(
        contract=contract,
        operations={
            MODEL_TOOL_OPERATION: OperationSpec(
                resource_type=ResourceType.TOOL_ACTION.value,
                resource_operation=Operation.CREATE.value,
            )
        },
        adapters={ExecutionPlatform.SERVER: ModelToolAdapter(provider)},
        is_model_tool=True,
        projected_cost_per_call=spec.projected_cost(prompt_chars=MAX_PROMPT_CHARS),
    )
