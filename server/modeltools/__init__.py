"""LLM-as-a-tool — 06_MODEL_PROVIDER_LLM_TOOL.md §3 (MODELTOOL-001..004).

A model-tool is an ordinary tool whose adapter invokes a `ModelProvider`. It is
registered in the same `ToolRegistry`, gated by the same capability
(`model.invoke`), authorized by the same engine, and metered as a `model_call`
(02 §6). Its output is **untrusted data** (06 §4): the runtime feeds it back as
an observation, and any action the agent proposes because of it is authorized
from scratch.

It is native, not MCP (MODELTOOL-003), and single-shot: it takes a prompt and
returns text, so it cannot itself propose tool calls. The runtime's
`max_model_tool_nesting_depth` bound still applies (OD-RT-1).
"""

from server.modeltools.adapter import (
    MODEL_TOOL_CAPABILITY,
    ModelToolAdapter,
    ModelToolParts,
    model_tool_definition,
)

__all__ = ["MODEL_TOOL_CAPABILITY", "ModelToolAdapter", "ModelToolParts", "model_tool_definition"]
