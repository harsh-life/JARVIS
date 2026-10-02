"""The persisted lifecycle row for a task (`agent_tasks`).

Status, counters, and the final response or failure code only — never the
transcript (see `state.py`). Reads are owner-scoped by the caller.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.storage.models import AgentRunRow, AgentTask, StandingDelegationRow
from shared.schemas.agent import AgentTaskStatus, TERMINAL_STATUSES
from shared.schemas.authorization import AnyPrincipal, DelegatedPrincipal, Principal

MAX_RESPONSE_CHARS = 20_000
_NON_TERMINAL = [s.value for s in AgentTaskStatus if s not in TERMINAL_STATUSES]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def create_task_row(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
    device_id: uuid.UUID | None,
    session_id: uuid.UUID | None,
    graph_id: uuid.UUID | None,
    mode: str = "execute",
    delegation_id: uuid.UUID | None = None,
) -> AgentTask:
    """A present user's task carries its device and session; an unattended
    run's (docs/29 §15.2) carries its delegation and neither — the store's
    `ck_agent_tasks_principal` refuses anything else."""

    now = _utcnow()
    row = AgentTask(
        task_id=task_id,
        user_id=user_id,
        device_id=device_id,
        session_id=session_id,
        delegation_id=delegation_id,
        graph_id=graph_id,
        status=AgentTaskStatus.RUNNING.value,
        mode=mode,
        iterations=0,
        worker_switches=0,
        model_calls=0,
        tool_calls=0,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def load_task_row(session: AsyncSession, task_id: uuid.UUID) -> AgentTask | None:
    return await session.get(AgentTask, task_id)


async def update_task_row(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    status: AgentTaskStatus,
    iterations: int,
    model_calls: int,
    tool_calls: int,
    response: str | None = None,
    failure_code: str | None = None,
    worker_switches: int | None = None,
) -> AgentTask | None:
    row = await session.get(AgentTask, task_id)
    if row is None:
        return None
    now = _utcnow()
    row.status = status.value
    row.iterations = iterations
    row.model_calls = model_calls
    row.tool_calls = tool_calls
    if response is not None:
        row.response = response[:MAX_RESPONSE_CHARS]
    if failure_code is not None:
        row.failure_code = failure_code
    if worker_switches is not None:
        row.worker_switches = worker_switches
    row.updated_at = now
    if status in TERMINAL_STATUSES:
        row.finished_at = now
    await session.flush()
    return row


async def close_if_live(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    status: AgentTaskStatus,
    failure_code: str | None = None,
) -> tuple[AgentTask | None, bool]:
    """Move a task to a terminal status **only if it is not terminal yet**.

    For the paths that close a task from its row, with no live state to go by
    (a restart, a pruned pause, a stateless cancel or stop). The row they read
    can be stale: another request may be finishing the same task right now,
    holding the store's write lock. This single conditional UPDATE runs after
    that request commits, sees its outcome, and changes nothing if it already
    ended — so a completed task is never rewritten as failed or cancelled.

    Returns the row as it now is, and whether this call closed it.
    """

    assert status in TERMINAL_STATUSES
    now = _utcnow()
    values: dict = {"status": status.value, "updated_at": now, "finished_at": now}
    if failure_code is not None:
        values["failure_code"] = failure_code
    result = await session.execute(
        update(AgentTask)
        .where(AgentTask.task_id == task_id, AgentTask.status.in_(_NON_TERMINAL))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    row = (await session.execute(
        select(AgentTask).where(AgentTask.task_id == task_id).execution_options(populate_existing=True)
    )).scalar_one_or_none()
    return row, result.rowcount == 1


async def non_terminal_task_ids(session: AsyncSession) -> list[uuid.UUID]:
    return list((await session.execute(
        select(AgentTask.task_id).where(AgentTask.status.in_(_NON_TERMINAL))
    )).scalars().all())


async def row_principal(session: AsyncSession, row: AgentTask) -> AnyPrincipal:
    """The principal a task row was created for — to close and audit that row
    when no live state holds it (a stop, a restart). A present user's row
    yields its `Principal`; an unattended run's yields its
    `DelegatedPrincipal` (docs/29 §15.2), never a device or session. Used only
    to close and record, never to authorize anything."""

    if row.delegation_id is None:
        return Principal(user_id=row.user_id, device_id=row.device_id, session_id=row.session_id,
                         active_graph_id=row.graph_id)
    delegation = await session.get(StandingDelegationRow, row.delegation_id)
    run_id = (await session.execute(
        select(AgentRunRow.run_id).where(AgentRunRow.task_id == row.task_id))).scalars().first()
    return DelegatedPrincipal(
        user_id=row.user_id,
        agent_id=delegation.agent_id if delegation is not None else row.task_id,
        delegation_id=row.delegation_id,
        # A run whose record was never written still closes: the task id
        # stands in for it in the audit trail.
        run_id=run_id or row.task_id,
        graph_id=row.graph_id,
    )
