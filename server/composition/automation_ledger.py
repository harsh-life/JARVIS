"""The durable idempotency record for n8n workflow calls (docs/32 §4).

Implements `server.execution.automation.InvocationLedger` on the
`automation_invocations` table. Every method opens its own session and commits
before it returns: a reservation is durable **before** the request is sent,
independent of the task's own request transaction (which H-1 has already
released before a tool runs). Concurrency is decided by the primary key: of two
callers inserting the same key, exactly one succeeds; the other reads the row.

Transitions that re-open a key are conditional updates, so only one caller can
win them:

* `failed_before_send` → `reserved`: the previous attempt provably sent nothing;
* `reserved` older than `stale_after` → `reserved`: its holder never marked it
  `in_progress` (crashed before sending), so nothing was sent.

Nothing else re-opens a key. `in_progress` is never re-opened: a crash after
it was committed is an uncertain outcome, not a retry.

Retention: rows older than `retention` (30 days by default) are deleted the
next time a key is reserved — bounded, opportunistic, no background job.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from sqlalchemy import delete, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.execution.automation import InvocationState, LedgerEntry
from server.storage import StorageBackend
from server.storage.errors import is_transient_store_error
from server.storage.models import AutomationInvocationRow


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class StorageInvocationLedger:
    def __init__(self, storage: StorageBackend, *, stale_after: timedelta, retention: timedelta = timedelta(days=30),
                 busy_timeout_seconds: float = 30.0, clock: Callable[[], datetime] = _utcnow) -> None:
        self._storage = storage
        self._stale_after = stale_after
        self._retention = retention
        self._busy_timeout = busy_timeout_seconds
        self._clock = clock

    async def _write(self, work: Callable[[AsyncSession], Awaitable[object]]):
        """One committed transaction, retried while the store is busy."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._busy_timeout
        delay = 0.05
        while True:
            try:
                async with self._storage.session() as session:
                    result = await work(session)
                    await session.commit()
                    return result
            except DBAPIError as exc:
                if isinstance(exc, IntegrityError) or not is_transient_store_error(exc) or loop.time() >= deadline:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 1.0)

    async def reserve(self, key: str, *, user_id: uuid.UUID, task_id: uuid.UUID | None,
                      workflow_id: str) -> LedgerEntry | None:
        now = self._clock()

        async def prune(session: AsyncSession) -> None:
            await session.execute(delete(AutomationInvocationRow).where(
                AutomationInvocationRow.updated_at < now - self._retention))

        await self._write(prune)

        async def insert(session: AsyncSession) -> None:
            session.add(AutomationInvocationRow(
                idempotency_key=key, user_id=user_id, task_id=task_id, workflow_id=workflow_id,
                state=InvocationState.RESERVED.value, created_at=now, updated_at=now))
            await session.flush()

        try:
            await self._write(insert)
            return None
        except IntegrityError:
            pass  # the key exists: read it, and re-open it only if provably unsent

        async def reopen(session: AsyncSession) -> int:
            result = await session.execute(
                update(AutomationInvocationRow)
                .where(AutomationInvocationRow.idempotency_key == key)
                .where(
                    (AutomationInvocationRow.state == InvocationState.FAILED_BEFORE_SEND.value)
                    | ((AutomationInvocationRow.state == InvocationState.RESERVED.value)
                       & (AutomationInvocationRow.updated_at < now - self._stale_after)))
                .values(state=InvocationState.RESERVED.value, error_code=None, result_content=None, updated_at=now)
                .execution_options(synchronize_session=False))
            return int(result.rowcount or 0)

        if await self._write(reopen) == 1:
            return None
        async with self._storage.session() as session:
            row = await session.get(AutomationInvocationRow, key)
            if row is None:  # pruned between the two statements: treat as in progress, never re-run
                return LedgerEntry(InvocationState.IN_PROGRESS)
            if row.user_id != user_id:  # a key collision across users is not a replay
                return LedgerEntry(InvocationState.IN_PROGRESS)
            return LedgerEntry(InvocationState(row.state), row.result_content, row.error_code)

    async def record(self, key: str, state: InvocationState, *, content: str | None = None,
                     error_code: str | None = None) -> None:
        now = self._clock()

        async def write(session: AsyncSession) -> None:
            await session.execute(
                update(AutomationInvocationRow).where(AutomationInvocationRow.idempotency_key == key)
                .values(state=state.value, result_content=content, error_code=error_code, updated_at=now)
                .execution_options(synchronize_session=False))

        await self._write(write)


def automation_ledger_for(config, storage: StorageBackend) -> "StorageInvocationLedger | None":
    """The durable ledger, when n8n is enabled. A reservation is stale (its
    holder died before sending) after twice the longest workflow timeout plus
    a minute — longer than any live attempt can hold it unsent."""

    n8n = config.automation.n8n
    if not n8n.enabled:
        return None
    longest = max((w.timeout_seconds for w in n8n.workflows), default=20.0)
    return StorageInvocationLedger(storage, stale_after=timedelta(seconds=2 * longest + 60))


__all__ = ["StorageInvocationLedger", "automation_ledger_for"]
