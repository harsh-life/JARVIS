"""LLM-as-a-Tool — 06_MODEL_PROVIDER_LLM_TOOL.md §3.

See `server/modeltools/executor.py` for the implementation. Independent of
`server.tools` under the layering contract (both occupy the same sibling
band) — the gateway composition root registers a `ModelToolExecutor` into
`server.tools.ToolRegistry` under its own `tool_id`; neither package imports
the other.
"""

from server.modeltools.executor import ModelToolExecutor

__all__ = ["ModelToolExecutor"]
