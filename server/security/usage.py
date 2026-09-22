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

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.storage.models import UsageEvent
from shared.schemas.enums import UsageKind

logger = logging.getLogger("hypermind.security.usage")

RATE_WINDOW = timedelta(minutes=1)
BUDGET_WINDOW = timedelta(days=1)


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
    ) -> None:
        session.add(
            UsageEvent(
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
        )
        await session.flush()

    async def calls_since(
        self,
        session: AsyncSession,
        *,
        since: datetime,
        user_id: uuid.UUID | None = None,
        device_id: uuid.UUID | None = None,
    ) -> int:
        query = select(func.count()).select_from(UsageEvent).where(UsageEvent.timestamp >= since)
        if user_id is not None:
            query = query.where(UsageEvent.user_id == user_id)
        if device_id is not None:
            query = query.where(UsageEvent.device_id == device_id)
        return int((await session.execute(query)).scalar_one())

    async def cost_since(
        self, session: AsyncSession, *, since: datetime, user_id: uuid.UUID | None = None
    ) -> float:
        query = select(func.coalesce(func.sum(UsageEvent.estimated_cost), 0.0)).where(
            UsageEvent.timestamp >= since
        )
        if user_id is not None:
            query = query.where(UsageEvent.user_id == user_id)
        return float((await session.execute(query)).scalar_one())


class UsagePolicy:
    """Rate and budget checks, evaluated against the ledger before a call."""

    def __init__(self, *, limits: UsageLimits, ledger: UsageLedger | None = None) -> None:
        self._limits = limits
        self._ledger = ledger or UsageLedger()

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

        if await self._ledger.calls_since(session, since=rate_since, user_id=user_id) >= (
            limits.per_user_calls_per_minute
        ):
            raise LimitExceeded("per_user_rate", retry_after_seconds=retry)

        if device_id is not None and await self._ledger.calls_since(
            session, since=rate_since, device_id=device_id
        ) >= limits.per_device_calls_per_minute:
            raise LimitExceeded("per_device_rate", retry_after_seconds=retry)

        if await self._ledger.calls_since(session, since=rate_since) >= (
            limits.global_calls_per_minute
        ):
            raise LimitExceeded("global_rate", retry_after_seconds=retry)

        if projected_cost > 0:
            budget_since = now - BUDGET_WINDOW
            retry_budget = int(BUDGET_WINDOW.total_seconds())
            user_spent = await self._ledger.cost_since(session, since=budget_since, user_id=user_id)
            if user_spent + projected_cost > limits.per_user_daily_cost_limit:
                raise LimitExceeded("per_user_budget", retry_after_seconds=retry_budget)
            global_spent = await self._ledger.cost_since(session, since=budget_since)
            if global_spent + projected_cost > limits.global_daily_cost_limit:
                raise LimitExceeded("global_budget", retry_after_seconds=retry_budget)
