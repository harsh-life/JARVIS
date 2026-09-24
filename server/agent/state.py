"""Session-volatile task state (05 §3, 11 §1, MEM-001).

The working transcript, counters, activated capabilities, and a paused action
live **here, in process memory**, and are discarded at task end. None of it is
written to a database: 11 §1 `[LOCKED]` — "the moment session state would be
written to disk/DB, it belongs in Mem0 (with a visibility decision)". A process
restart therefore loses a paused task's action, and the task fails closed
(`confirmation_state_lost`) rather than executing anything.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from server.models.provider import ChatMessage
from shared.schemas.agent import ExecutionPlatform, TaskMode
from shared.schemas.authorization import Operation, Principal, ResourceType
from shared.schemas.enums import RiskCategory

if TYPE_CHECKING:
    from server.agent.breaker import Trip


@dataclass(frozen=True)
class Activation:
    """A capability active for this task. `source` records *why*: the user's
    standing grant, or a task-scoped grant the user approved for this task."""

    capability: str
    resource_scope: dict[str, str] | None
    source: str  # "standing" | "task_grant"


@dataclass
class PendingStep:
    """The one action awaiting the human. Held only in memory."""

    kind: str  # "tool_operation" | "capability_activation"
    capability: str
    risk_category: RiskCategory
    token: str
    expires_at: datetime
    tool_id: str | None = None
    operation: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    resource_ref: str | None = None
    platform: ExecutionPlatform | None = None
    resource_scope: dict[str, str] | None = None
    resource_type: ResourceType | None = None
    resource_operation: Operation | None = None

    def expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(timezone.utc)) >= self.expires_at


@dataclass
class TaskState:
    task_id: uuid.UUID
    principal: Principal
    graph_id: uuid.UUID | None
    # 18 §3: fixed at submission; nothing assigns it afterwards.
    mode: TaskMode = TaskMode.EXECUTE
    messages: list[ChatMessage] = field(default_factory=list)
    activations: list[Activation] = field(default_factory=list)
    iterations: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    consecutive_parse_failures: int = 0
    cost: float = 0.0
    run_seconds_used: float = 0.0
    pending: PendingStep | None = None
    cancelled: bool = False
    # Set by `/cancel` and by a breaker trip; an in-flight tool call races
    # against it (05 §9).
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    # 18 §5: set once by the circuit breaker and never cleared. A tripped task
    # is stopped at its next checkpoint and can never be resumed or confirmed.
    tripped: Trip | None = None
    # Set once a stop is being enforced, so two requests racing to enforce the
    # same stop (an operator stop and the owner's /confirm) do it once.
    stop_enforced: bool = False
    # The breaker's in-task trigger counters (18 §5.1).
    denials: int = 0
    violations: int = 0
    rejections: int = 0
    notes: list[str] = field(default_factory=list)
    allowed_tool_ids: frozenset[str] | None = None
    created_monotonic: float = field(default_factory=time.monotonic)

    def active_capability_names(self) -> list[str]:
        return sorted({a.capability for a in self.activations})

    def find_activation(
        self, capability: str, scope: dict[str, str] | None
    ) -> Activation | None:
        candidates = [a for a in self.activations if a.capability == capability]
        if scope is not None:
            for activation in candidates:
                if (activation.resource_scope or None) == (scope or None):
                    return activation
            return None
        if len(candidates) == 1:
            return candidates[0]
        # Ambiguous: more than one narrowing is active and the proposal did not
        # say which. Refused rather than guessed.
        return None

    def has_activation(self, capability: str, scope: dict[str, str] | None) -> bool:
        return any(
            a.capability == capability and (a.resource_scope or None) == (scope or None)
            for a in self.activations
        )


class TaskStateRegistry:
    """Process-local map of live tasks. Bounded: running tasks are capped by the
    concurrency gate, and paused ones expire with their confirmation token."""

    def __init__(self) -> None:
        self._states: dict[uuid.UUID, TaskState] = {}

    def get(self, task_id: uuid.UUID) -> TaskState | None:
        return self._states.get(task_id)

    def put(self, state: TaskState) -> None:
        self._states[state.task_id] = state

    def pop(self, task_id: uuid.UUID) -> TaskState | None:
        return self._states.pop(task_id, None)

    def live(self) -> list[TaskState]:
        """A snapshot of every task live in this process (running or paused)."""

        return list(self._states.values())

    def prune_expired(self) -> None:
        now = datetime.now(timezone.utc)
        for task_id in [
            tid for tid, s in self._states.items() if s.pending is not None and s.pending.expired(now)
        ]:
            self._states.pop(task_id, None)

    def __len__(self) -> int:
        return len(self._states)
