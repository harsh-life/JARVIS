"""Bounded tool execution (07 §5 "Resource → per-contract timeout").

An adapter failure — an exception, a timeout, a malformed return — becomes a
failed `ToolOutput` that the runtime feeds back to the model as an observation
(FAIL-006). It never crashes the runtime and never becomes a success.
"""

from __future__ import annotations

import asyncio
import logging

from shared.schemas.agent import ToolInvocation, ToolOutput

logger = logging.getLogger("hypermind.tools.executor")


async def run_tool(adapter, invocation: ToolInvocation, *, timeout: float) -> ToolOutput:
    try:
        output = await asyncio.wait_for(adapter.execute(invocation), timeout=max(0.001, timeout))
    except asyncio.TimeoutError:
        return ToolOutput(ok=False, error="timeout")
    except Exception as exc:  # noqa: BLE001 — a tool failure is an observation (FAIL-006)
        logger.warning("tool %s failed: %s", invocation.tool_id, type(exc).__name__)
        return ToolOutput(ok=False, error=f"tool_error:{type(exc).__name__}")

    if not isinstance(output, ToolOutput):
        return ToolOutput(ok=False, error="malformed_tool_output")
    return output
