"""The circuit breaker — deterministic emergency control (18 §5).

    trigger → breaker.trip(scope, target_id, reason, source) → the runtime
    enforces the stop at the task's next checkpoint (18 §5.3)

Three rules this module exists to hold (18 §0, §5.2):

* **Stopping is deterministic and has many independent inputs.** Every trigger
  here is plain counting over facts the runtime already produced — an
  authorization denial, a boundary refusal from the execution layer, a user's
  rejection of a confirmation. No model, and no evaluator, is consulted.
* **One-way.** A trip can only stop. There is no method that clears a trip,
  resumes a task, grants a capability, lowers a tier, or satisfies a
  confirmation. The first trip on a task is the one recorded; a later one
  changes nothing.
* **Not the executor of the stop.** `trip()` only marks the task and signals
  its cancel event (which aborts an in-flight tool call and kills its process
  group, OD-RT-4). Marking the task terminal, invalidating its confirmation
  tokens, revoking its task grants, releasing its temp root and auditing are
  done by the runtime (`AgentRuntime._emergency_stop`), because they need the
  request's transaction.

Scope of this build unit (U1): task-scoped trips from the three in-task
triggers. Operator stops at user/device/global scope and the global latch
(18 §5.4) and the evaluator trigger (19 §6) are later units; they will call the
same `trip()`.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from server.agent.state import TaskState, TaskStateRegistry
from shared.schemas.execution import ExecutionErrorCode


class BreakerScope(str, Enum):
    TASK = "task"


class TripSource(str, Enum):
    """Which independent input fired. Recorded in the audit event."""

    DENIAL_LIMIT = "denial_limit"
    VIOLATION_LIMIT = "violation_limit"
    REJECTION_LIMIT = "rejection_limit"


# Refusals by an execution boundary (09 sandbox, 10 egress, the process
# executor's allow-list) — the "boundary violations" 18 §5.1 counts. Ordinary
# operational failures (a timeout, an unresolvable host, malformed arguments,
# an unavailable device) are deliberately not violations: they are not an
# attempt to cross a boundary, and counting them would stop honest tasks.
BOUNDARY_VIOLATION_CODES: frozenset[str] = frozenset(
    code.value
    for code in (
        ExecutionErrorCode.SANDBOX_VIOLATION,
        ExecutionErrorCode.FORBIDDEN_PATH,
        ExecutionErrorCode.EGRESS_DENIED,
        ExecutionErrorCode.UNAUTHORIZED_EXECUTABLE,
    )
)

# The audit trail has no free-form payload column (SECRET-004), so a trip's
# reason is an identifier, never prose that could carry task content.
_REASON_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


@dataclass(frozen=True)
class BreakerLimits:
    """`agent.breaker` (18 §9). Bounds, not authority."""

    denial_limit: int = 5
    violation_limit: int = 3
    rejection_limit: int = 3


@dataclass(frozen=True)
class Trip:
    scope: BreakerScope
    target_id: uuid.UUID
    reason: str
    source: str
    at: datetime


class CircuitBreaker:
    def __init__(self, limits: BreakerLimits, states: TaskStateRegistry) -> None:
        for name in ("denial_limit", "violation_limit", "rejection_limit"):
            if getattr(limits, name) < 1:
                raise ValueError(f"breaker {name} must be at least 1")
        self._limits = limits
        self._states = states

    @property
    def limits(self) -> BreakerLimits:
        return self._limits

    # ── the port (18 §5.3) ──────────────────────────────────────────────

    def trip(self, scope: BreakerScope, target_id: uuid.UUID, reason: str, source: str) -> None:
        """Stop a task. Idempotent and irreversible: the first trip wins.

        A target that is not live in this process (already terminal, or never
        existed) is a no-op — there is nothing running to stop.
        """

        if scope is not BreakerScope.TASK:
            raise ValueError(f"unsupported breaker scope: {scope!r}")
        if not _REASON_PATTERN.fullmatch(reason) or not _REASON_PATTERN.fullmatch(source):
            raise ValueError("breaker reason and source must be short identifiers")
        state = self._states.get(target_id)
        if state is None:
            return
        _mark(state, scope, reason, source)

    # ── in-task triggers (18 §5.1) ──────────────────────────────────────

    def record_denial(self, state: TaskState) -> None:
        state.denials += 1
        if state.denials >= self._limits.denial_limit:
            self._trip_task(state, TripSource.DENIAL_LIMIT)

    def record_tool_outcome(self, state: TaskState, *, ok: bool, error: str | None) -> None:
        if ok or error not in BOUNDARY_VIOLATION_CODES:
            return
        state.violations += 1
        if state.violations >= self._limits.violation_limit:
            self._trip_task(state, TripSource.VIOLATION_LIMIT)

    def record_rejection(self, state: TaskState) -> None:
        state.rejections += 1
        if state.rejections >= self._limits.rejection_limit:
            self._trip_task(state, TripSource.REJECTION_LIMIT)

    def _trip_task(self, state: TaskState, source: TripSource) -> None:
        # The task in hand, not a registry lookup: an in-task trigger fires
        # while the runtime is driving exactly this state.
        _mark(state, BreakerScope.TASK, source.value, source.value)


def _mark(state: TaskState, scope: BreakerScope, reason: str, source: str) -> None:
    if state.tripped is None:
        state.tripped = Trip(scope, state.task_id, reason, source, datetime.now(timezone.utc))
    # Aborts an in-flight tool call (its process group is killed) and keeps a
    # new one from starting (`AgentRuntime._run_cancellable`).
    state.cancel_event.set()
