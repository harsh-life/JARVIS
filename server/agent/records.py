"""The persisted lifecycle row for a task (`agent_tasks`).

Status, counters, and the final response or failure code only — never the
transcript (see `state.py`). Reads are owner-scoped by the caller.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from server.storage.models import AgentTask
from shared.schemas.agent import AgentTaskStatus, TERMINAL_STATUSES

MAX_RESPONSE_CHARS = 20_000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def create_task_row(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
    device_id: uuid.UUID,
    session_id: uuid.UUID,
    graph_id: uuid.UUID | None,
    mode: str = "execute",
) -> AgentTask:
    now = _utcnow()
    row = AgentTask(
        task_id=task_id,
        user_id=user_id,
        device_id=device_id,
        session_id=session_id,
        graph_id=graph_id,
        status=AgentTaskStatus.RUNNING.value,
        mode=mode,
        iterations=0,
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
    row.updated_at = now
    if status in TERMINAL_STATUSES:
        row.finished_at = now
    await session.flush()
    return row
