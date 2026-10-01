"""The usage ledger and the limits enforced against it (13_USAGE_RATE_BUDGET.md).

13 §0, the rule this module exists to enforce:

> A runaway or malicious agent must not be able to consume unlimited API money,
> CPU, RAM, network, tool executions, or scheduler jobs — and every consuming
> call is metered so limits are enforced against real accounting, not guesses.

Three properties, and where each lives:

* **One ledger.** `UsageLedger.record` writes exactly one `UsageEvent` per model
  call or tool execution (USAGE-001). Rates and budgets are *queries over that
  ledger* (USAGE-002) — there is no second counter that could drift from it.
* **Nothing secret in it.** A `UsageEvent` has ids, a provider/model/tool name,
  a unit count and a cost. `record` takes nothing else, so there is no argument
  through which a credential could reach the table (USAGE-003, INV-16).
* **Never fails open.** If the ledger cannot be read, `precheck` raises
  `LimitExceeded("limiter_unavailable")`: 13 §4 — "if the counter can't be
  read, treat as at-limit rather than unlimited".

Limits are per principal (user, device) plus global, so one user's runaway is
contained to their own quota (13 §6). The numbers are `[IMPL]` / OD-USE-1 and
come from configuration.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.storage.models import ScheduledJob, UsageEvent
from shared.schemas.enums import JobStatus, UsageKind
from shared.schemas.evaluation import EVALUATOR_USAGE_PREFIX

logger = logging.getLogger("hypermind.security.usage")

RATE_WINDOW = timedelta(minutes=1)
BUDGET_WINDOW = timedelta(days=1)
SCHEDULER_CREATION_WINDOW = timedelta(hours=1)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LimitExceeded(Exception):
    """A deterministic limit refused the call (FAIL-003, `429 rate_limited`).

    `limit` names which one, so the failure is explicit about *why* (13 §5).
    """

    def __init__(self, limit: str, *, retry_after_seconds: int) -> None:
        super().__init__(f"limit exceeded: {limit}")
        self.limit = limit
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class UsageLimits:
    """OD-USE-1 values. A `0.0` cost limit means *no paid spend at all*: a call
    with a positive projected cost is refused, and a free local call is not."""

    per_user_calls_per_minute: int
    per_device_calls_per_minute: int
    global_calls_per_minute: int
    per_user_daily_cost_limit: float
    global_daily_cost_limit: float
    # 19 §8 / OD-JDG-2: Judge spend never counts toward a user's own budget or
    # any rate; whether it counts toward the global budget is this switch
    # (`evaluation.budget_scope`), defaulting to the more restrictive reading.
    evaluation_counts_toward_global_budget: bool = True


class UsageLedger:
    """Append-only metering (01 §11.2)."""

    async def record(
        self,
        session: AsyncSession,
        *,
        request_id: uuid.UUID,
        user_id: uuid.UUID,
        kind: UsageKind,
        units: int,
        estimated_cost: float,
        device_id: uuid.UUID | None = None,
        session_id: uuid.UUID | None = None,
        graph_id: uuid.UUID | None = None,
        provider: str | None = None,
        model: str | None = None,
        tool_id: str | None = None,
    ) -> uuid.UUID:
        """Add one ledger row; returns its `usage_id` (what an attribution,
        e.g. an agent run's, joins on — docs/29 §17)."""

        event = UsageEvent(
            request_id=request_id,
            user_id=user_id,
            device_id=device_id,
            session_id=session_id,
            graph_id=graph_id,
            kind=kind,
            provider=provider,
            model=model,
            tool_id=tool_id,
            tokens_or_units=max(0, int(units)),
            estimated_cost=max(0.0, float(estimated_cost)),
            timestamp=_utcnow(),
        )
        session.add(event)
        await session.flush()
        return event.usage_id

    async def calls_since(
        self,
        session: AsyncSession,
        *,
        since: datetime,
        user_id: uuid.UUID | None = None,
        device_id: uuid.UUID | None = None,
        exclude_evaluation: bool = False,
    ) -> int:
        query = select(func.count()).select_from(UsageEvent).where(UsageEvent.timestamp >= since)
        if exclude_evaluation:
            query = query.where(_not_evaluation())
        if user_id is not None:
            query = query.where(UsageEvent.user_id == user_id)
        if device_id is not None:
            query = query.where(UsageEvent.device_id == device_id)
        return int((await session.execute(query)).scalar_one())

    async def cost_since(
        self, session: AsyncSession, *, since: datetime, user_id: uuid.UUID | None = None,
        exclude_evaluation: bool = False,
    ) -> float:
        query = select(func.coalesce(func.sum(UsageEvent.estimated_cost), 0.0)).where(
            UsageEvent.timestamp >= since
        )
        if exclude_evaluation:
            query = query.where(_not_evaluation())
        if user_id is not None:
            query = query.where(UsageEvent.user_id == user_id)
        return float((await session.execute(query)).scalar_one())


@dataclass
class _Admission:
    """A call admitted but not yet visible in the ledger to other requests."""

    user_id: uuid.UUID
    device_id: uuid.UUID | None
    cost: float
    expires: float
    # The session whose own (uncommitted) ledger row now stands for this call:
    # that session's queries already count it, every other session's do not.
    recorded_in: object | None = None


class UsagePolicy:
    """Rate and budget checks, evaluated against the ledger before a call.

    H-1: the ledger alone is not enough once requests run concurrently and
    commit early. Between a call's admission and its ledger row becoming
    visible (the call itself, then a commit), another request's check would
    not see it, and two calls could both pass a limit with room for one. So an
    admission (`admit`) is held in process until the row that accounts for it
    is committed — or the transaction that would have written it is gone — and
    every check counts the held admissions alongside the ledger. The pilot is
    one server process (docs/RUNNING_RUNTIME.md §4a), so this is the whole set
    of calls in flight.
    """

    # A safety net only: an admission is normally let go when its request's
    # transaction ends. One never let go (a bug) stops counting after this.
    ADMISSION_TTL_SECONDS = 900.0

    def __init__(self, *, limits: UsageLimits, ledger: UsageLedger | None = None) -> None:
        self._limits = limits
        self._ledger = ledger or UsageLedger()
        self._admitting = asyncio.Lock()
        self._admitted: dict[int, _Admission] = {}
        self._ids = itertools.count(1)

    @property
    def ledger(self) -> UsageLedger:
        return self._ledger

    async def precheck(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        device_id: uuid.UUID | None,
        projected_cost: float,
    ) -> None:
        """Refuse the call if it would breach any limit. Returns only if it may
        proceed; every refusal is an explicit `LimitExceeded` (13 §5)."""

        try:
            await self._check(
                session, user_id=user_id, device_id=device_id, projected_cost=projected_cost
            )
        except LimitExceeded:
            raise
        except Exception:  # noqa: BLE001 — fail closed (13 §4)
            logger.exception("usage ledger unreadable; treating as at-limit")
            raise LimitExceeded("limiter_unavailable", retry_after_seconds=60) from None

    async def admit(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        device_id: uuid.UUID | None,
        projected_cost: float,
    ) -> int:
        """`precheck`, and on success hold the call as admitted until
        `release`. One admission is checked at a time, so two cannot both
        pass on room for one. Returns the admission's id."""

        async with self._admitting:
            await self.precheck(session, user_id=user_id, device_id=device_id,
                                projected_cost=projected_cost)
            admission_id = next(self._ids)
            self._admitted[admission_id] = _Admission(
                user_id=user_id, device_id=device_id, cost=max(0.0, float(projected_cost)),
                expires=time.monotonic() + self.ADMISSION_TTL_SECONDS,
            )
            return admission_id

    def recorded(self, admission_id: int, session: object) -> None:
        """The admitted call's ledger row is written in `session`, uncommitted."""

        admission = self._admitted.get(admission_id)
        if admission is not None:
            admission.recorded_in = session

    def release(self, admission_id: int) -> None:
        self._admitted.pop(admission_id, None)

    def _in_flight(self, session: AsyncSession) -> list[_Admission]:
        now = time.monotonic()
        for admission_id in [i for i, a in self._admitted.items() if a.expires <= now]:
            logger.warning("usage admission %s was never released; no longer counted", admission_id)
            del self._admitted[admission_id]
        own = getattr(session, "sync_session", session)
        return [a for a in self._admitted.values() if a.recorded_in is not own]

    async def _check(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        device_id: uuid.UUID | None,
        projected_cost: float,
    ) -> None:
        now = _utcnow()
        limits = self._limits
        rate_since = now - RATE_WINDOW
        retry = int(RATE_WINDOW.total_seconds())
        in_flight = self._in_flight(session)
        mine = [a for a in in_flight if a.user_id == user_id]

        if await self._ledger.calls_since(session, since=rate_since, user_id=user_id,
                                          exclude_evaluation=True) + len(mine) >= (
            limits.per_user_calls_per_minute
        ):
            raise LimitExceeded("per_user_rate", retry_after_seconds=retry)

        if device_id is not None and await self._ledger.calls_since(
            session, since=rate_since, device_id=device_id, exclude_evaluation=True
        ) + sum(1 for a in in_flight if a.device_id == device_id) >= limits.per_device_calls_per_minute:
            raise LimitExceeded("per_device_rate", retry_after_seconds=retry)

        if await self._ledger.calls_since(session, since=rate_since, exclude_evaluation=True) + len(
            in_flight
        ) >= limits.global_calls_per_minute:
            raise LimitExceeded("global_rate", retry_after_seconds=retry)

        if projected_cost > 0:
            budget_since = now - BUDGET_WINDOW
            retry_budget = int(BUDGET_WINDOW.total_seconds())
            user_spent = await self._ledger.cost_since(session, since=budget_since, user_id=user_id,
                                                       exclude_evaluation=True)
            user_spent += sum(a.cost for a in mine)
            if user_spent + projected_cost > limits.per_user_daily_cost_limit:
                raise LimitExceeded("per_user_budget", retry_after_seconds=retry_budget)
            global_spent = await self._ledger.cost_since(
                session, since=budget_since,
                exclude_evaluation=not limits.evaluation_counts_toward_global_budget,
            )
            global_spent += sum(a.cost for a in in_flight)
            if global_spent + projected_cost > limits.global_daily_cost_limit:
                raise LimitExceeded("global_budget", retry_after_seconds=retry_budget)


def _not_evaluation():
    """Rows that are not Judge calls (19 §8). A `NULL` tool id is a model call
    of a task, and counts."""

    return (UsageEvent.tool_id.is_(None)) | (~UsageEvent.tool_id.like(f"{EVALUATOR_USAGE_PREFIX}%"))


@dataclass(frozen=True)
class SchedulerLimits:
    """docs/22 §4 / FAIL-010: the per-user scheduler quota."""

    max_active_jobs_per_user: int
    creations_per_hour: int


class SchedulerQuota:
    """The scheduler's quota, held to the same three properties as the usage
    ledger above: one record, queried not counted separately; explicit
    `LimitExceeded` (→ `429`) naming which limit; fail-closed.

    Its record is the `scheduled_jobs` table itself — every creation is a row
    and rows are never deleted (a cancel changes `status`), so "creations in
    the last hour" is a query over the same rows the scheduler fires from and
    cannot be reset by creating and cancelling. A reminder is neither a model
    call nor a tool execution, so it is not a `UsageEvent` (`01` §1.2's
    `usage.kind` is locked); the agent's `create_reminder` tool call is still
    metered as the tool call it is, by the runtime.
    """

    def __init__(self, limits: SchedulerLimits) -> None:
        self._limits = limits

    @property
    def limits(self) -> SchedulerLimits:
        return self._limits

    async def precheck_creation(
        self, session: AsyncSession, *, user_id: uuid.UUID, now: datetime | None = None
    ) -> None:
        try:
            await self._check(session, user_id=user_id, now=now or _utcnow())
        except LimitExceeded:
            raise
        except Exception:  # noqa: BLE001 — fail closed (13 §4)
            logger.exception("scheduler quota unreadable; treating as at-limit")
            raise LimitExceeded("limiter_unavailable", retry_after_seconds=60) from None

    async def _check(self, session: AsyncSession, *, user_id: uuid.UUID, now: datetime) -> None:
        active = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ScheduledJob)
                    .where(ScheduledJob.owner_user_id == user_id, ScheduledJob.status == JobStatus.ACTIVE)
                )
            ).scalar_one()
        )
        if active >= self._limits.max_active_jobs_per_user:
            # Frees up only when the user cancels one or one fires for good.
            raise LimitExceeded("scheduler_active_jobs", retry_after_seconds=3600)

        since = now.astimezone(timezone.utc) - SCHEDULER_CREATION_WINDOW
        window = (
            await session.execute(
                select(func.count(), func.min(ScheduledJob.created_at)).where(
                    ScheduledJob.owner_user_id == user_id, ScheduledJob.created_at >= since
                )
            )
        ).one()
        created, oldest = int(window[0]), window[1]
        if created >= self._limits.creations_per_hour:
            retry = 60
            if oldest is not None:
                oldest = oldest if oldest.tzinfo is not None else oldest.replace(tzinfo=timezone.utc)
                retry = max(1, int((oldest + SCHEDULER_CREATION_WINDOW - now).total_seconds()) + 1)
            raise LimitExceeded("scheduler_creations_per_hour", retry_after_seconds=retry)
