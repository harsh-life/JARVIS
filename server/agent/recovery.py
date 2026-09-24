"""Supervisory recovery — the worker chain (18 §4, §8).

JARVIS is the supervisor: it detects a worker's failure, switches to the next
eligible worker in the task's chain, and otherwise fails the task honestly. A
worker is replaceable cognition, never a principal. A switch changes only
*who proposes the next step*; the task's principal, graph, mode, capability
activations, pending confirmation, counters and bounds carry over unchanged,
and every proposal from every worker goes through the same parser, bounds,
metering, authorization, tiers, confirmation and execution boundaries.

Detection is deterministic and needs no evaluator:

| Condition | Detection |
|---|---|
| unavailable / timeout | `ModelUnavailable` / `asyncio.TimeoutError` |
| malformed | still unparseable after `max_parse_retries` |
| stall (no progress) | `stall_window` consecutive steps with no successful tool execution or capability activation |
| stall (loop) | the same `(tool, operation, canonical-args-hash)` proposed `loop_repeat_limit` times |
| unresolved | a final answer with `unresolved: true`, when `escalate_on_unresolved` |

A worker asking the user a question is an answer, not a failure.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class SwitchReason(str, Enum):
    UNAVAILABLE = "unavailable"
    MALFORMED = "malformed"
    STALL = "stall"
    LOOP = "loop"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class RecoveryPolicy:
    """`agent.recovery` (18 §9). Bounds, not authority."""

    max_worker_switches: int = 2
    escalate_on_unresolved: bool = False
    stall_window: int = 3
    loop_repeat_limit: int = 3

    def __post_init__(self) -> None:
        if self.max_worker_switches < 0 or self.stall_window < 1 or self.loop_repeat_limit < 2:
            raise ValueError("invalid recovery bounds")


def operation_key(tool: str, operation: str, platform: str, arguments: Mapping[str, Any] | None,
                  resource_ref: str | None, scope: Mapping[str, Any] | None) -> str:
    """The canonical identity of one proposed operation: key order and
    whitespace never make two identical calls look different."""

    canonical = json.dumps(
        {"tool": tool, "operation": operation, "platform": platform, "arguments": dict(arguments or {}),
         "resource_ref": resource_ref, "scope": dict(scope or {})},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
