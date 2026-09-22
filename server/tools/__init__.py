"""Tools — 07_TOOL_CAPABILITY_EXECUTION.md.

    Capability = authorization concept   (server/capabilities)
    Tool       = executable interface    (a ToolContract, registered here)
    Adapter    = platform implementation (one per ExecutionPlatform)

A tool runs only if it is registered with a `ToolContract`, enabled, its
operation is in its capability's enumerated mapping, an adapter exists for the
target platform, and the authorization engine allows it (07 §0). This package
owns the first four; it never decides authorization — the runtime asks the
engine, through the composition root.

This package may *read* the capability registry (to validate a contract), but
it cannot reach the grant, confirmation, or decision paths: pyproject's
"Tools/models cannot reach the grant, confirmation, or decision paths" contract.
A tool that could approve its own call would be 16 §5's self-authorization.
"""

from server.tools.executor import run_tool
from server.tools.registry import (
    ToolAdapter,
    ToolDefinition,
    ToolRegistrationError,
    ToolRegistry,
)

__all__ = [
    "ToolAdapter",
    "ToolDefinition",
    "ToolRegistrationError",
    "ToolRegistry",
    "run_tool",
]
