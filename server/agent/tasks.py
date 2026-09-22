"""Task lifecycle state — 05 §3/§9: session-scoped, in-process, discarded at
task end.

`[LOCKED]` (05 §3): "the runtime holds per-task counters for the task's
duration and discards them at task end — no cross-task memory lives in the
runtime." `01_DATA_MODEL_SCHEMA.md` has no `AgentTask` entity (verified: it is
not in the canonical schema), which is consistent with this — a task is
runtime state, not a persisted first-class entity, so `TaskStore` is a plain
in-process store rather than a repository over `server.storage` (which
`server.agent` cannot import anyway, see `pyproject.toml`).

**Honest limitation, stated once here rather than discovered by an operator:**
this store does not survive a process restart, and does not coordinate across
multiple gateway processes/workers. That is an accepted property of the
pilot's single-process deployment (the same one `docs/OD_A1_BR_T2.md` already
frames its blast-radius measurement around), not a hidden gap — durable/
cross-process task state is `[FUTURE]` work for whichever branch introduces a
real job queue, and is flagged in this branch's final report as a genuinely
open architectural question rather than silently assumed away.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from shared.schemas.authorization import AccessRequest, ActionBinding, Principal
from shared.schemas.runtime import AgentProposal, AgentResult, ModelMessage, TaskStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class PendingAction:
    """What `/confirm` needs to resume a paused task (05 §4).

    Storing the exact `AccessRequest` that was denied with
    `require_confirmation` means resuming never has to reconstruct — and
    therefore never risks reconstructing *differently* — the action a human
    is being asked to approve. `binding` is kept alongside for the runtime's
    own bookkeeping/events; the engine re-derives its own binding from
    `access_request` regardless (it does not trust a caller-supplied one).
    """

    proposal: AgentProposal
    access_request: AccessRequest
    binding: ActionBinding


@dataclass
class LoopState:
    """One task's mutable runtime state — exactly the "session-scoped state
    only" 05 §3 describes, held for the task's duration and nowhere else."""

    task_id: str
    principal: Principal
    messages: list[ModelMessage] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    iterations: int = 0
    tool_calls: int = 0
    model_calls: int = 0
    parse_retries: int = 0
    nesting_depth: int = 0
    cost_accrued: float = 0.0
    started_at: datetime = field(default_factory=_utcnow)
    cancel_requested: bool = False
    pending: PendingAction | None = None
    result: AgentResult | None = None

    def elapsed_seconds(self) -> float:
        return (_utcnow() - self.started_at).total_seconds()


class UnknownTask(Exception):
    """`task_id` does not name a task this process holds — either it never
    existed here, or (05 §9) it already reached a terminal state and its
    state was not retained beyond what `result` needs."""


class TaskConflict(Exception):
    """The task exists but is not in the state the requested operation
    needs (e.g. `/confirm` on a task that is not `AWAITING_CONFIRMATION`)."""


class TaskStore:
    """Async-safe in-process registry of `LoopState`, keyed by `task_id`.

    One instance is constructed at app startup (`server/gateway/runtime.py`,
    parallel to how `server/gateway/security.py` builds `SecurityCore` once)
    and shared across requests — the lock below is what makes concurrent
    requests touching the same task_id (a status poll racing a confirm)
    safe, not a source of a torn read.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, LoopState] = {}
        self._lock = asyncio.Lock()

    async def create(self, *, principal: Principal) -> LoopState:
        state = LoopState(task_id=str(uuid.uuid4()), principal=principal)
        async with self._lock:
            self._tasks[state.task_id] = state
        return state

    async def get(self, task_id: str) -> LoopState:
        async with self._lock:
            state = self._tasks.get(task_id)
        if state is None:
            raise UnknownTask(task_id)
        return state

    async def save(self, state: LoopState) -> None:
        async with self._lock:
            self._tasks[state.task_id] = state

    async def request_cancellation(self, task_id: str, *, requested_by: Principal) -> LoopState:
        """05 §9 `/cancel`. Only the task's own principal may cancel it —
        checked here (rather than trusted from the caller) because this is
        the one place in `server.agent` that stands in for an authorization
        decision; it is deliberately narrow (identity equality, nothing
        else) rather than a re-implementation of `04`.
        """

        async with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                raise UnknownTask(task_id)
            if state.principal.user_id != requested_by.user_id:
                # Reported the same as "not found" — a task_id is not a
                # capability, but there is no reason to confirm another
                # user's task exists to a caller who doesn't own it either
                # (mirrors 04 §7's anti-enumeration posture).
                raise UnknownTask(task_id)
            if state.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                raise TaskConflict(f"task {task_id} already reached a terminal state")
            state.cancel_requested = True
        return state


__all__ = ["LoopState", "PendingAction", "TaskConflict", "TaskStore", "UnknownTask"]
