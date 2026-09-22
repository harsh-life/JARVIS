"""Tool registry & dispatch — 07_TOOL_CAPABILITY_EXECUTION.md §1/§8.

This branch (runtime) implements the registry/dispatch machinery only — see
`server/tools/executor.py`'s module docstring for the explicit scope
boundary against the Execution branch, which owns every concrete tool
(filesystem, network, Android/device).
"""

from server.tools.executor import ToolExecutor
from server.tools.registry import RegisteredTool, ToolRegistry

__all__ = ["RegisteredTool", "ToolExecutor", "ToolRegistry"]
