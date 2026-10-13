"""`GET /api/v1/usage` — the caller's own spend and limits (pilot plan C6,
step M2; P15).

Read from the same append-only ledger the usage policy enforces against
(01 §11.2, 13 §2–§3), over the same windows: the per-user daily limit is a
rolling 24 hours, the per-user rate a rolling minute, and Judge calls never
count toward a user's own budget or rate (19 §8). A figure here is a report,
never an input to a decision — the policy recomputes its own on every call.

Only the caller's rows are counted. The global limit and other users' spend
are not reported: a user learning how much everyone else spent is a
cross-tenant disclosure, and nothing a user can act on.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.gateway.deps import get_db_session, get_principal
from server.security.usage import BUDGET_WINDOW, RATE_WINDOW, UsageLedger, UsageLimits
from server.storage.models import AgentDefinitionRow, AgentRunRow, AgentRunUsageRow, UsageEvent
from shared.schemas.authorization import Principal

router = APIRouter(tags=["usage"])

REPORT_WINDOW = timedelta(days=30)
MAX_AGENTS = 100


class AgentSpend(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: uuid.UUID
    name: str | None
    runs: int
    spent: float


class UsageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The per-user budget (13 §3): a rolling 24 hours, in the currency of the
    # operator's model prices. A limit of 0 means paid calls are refused.
    spent_24h: float
    daily_limit: float
    remaining_24h: float
    # The per-user rate (13 §2): metered model and tool calls in the last minute.
    calls_last_minute: int
    calls_per_minute_limit: int
    # A 30-day report, and the part of it each of the caller's agents caused.
    spent_30d: float
    agents_30d: list[AgentSpend]


def _limits(request: Request) -> UsageLimits | None:
    return getattr(request.app.state, "usage_limits", None)


@router.get("/usage", response_model=UsageResponse)
async def usage(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
) -> UsageResponse:
    now = datetime.now(timezone.utc)
    ledger = UsageLedger()
    user = principal.user_id
    spent_24h = await ledger.cost_since(session, since=now - BUDGET_WINDOW, user_id=user, exclude_evaluation=True)
    calls = await ledger.calls_since(session, since=now - RATE_WINDOW, user_id=user, exclude_evaluation=True)
    spent_30d = await ledger.cost_since(session, since=now - REPORT_WINDOW, user_id=user, exclude_evaluation=True)

    rows = (await session.execute(
        select(
            AgentRunRow.agent_id,
            AgentDefinitionRow.name,
            func.count(func.distinct(AgentRunRow.run_id)),
            func.coalesce(func.sum(UsageEvent.estimated_cost), 0.0),
        )
        .select_from(AgentRunUsageRow)
        .join(UsageEvent, UsageEvent.usage_id == AgentRunUsageRow.usage_id)
        .join(AgentRunRow, AgentRunRow.run_id == AgentRunUsageRow.run_id)
        .join(AgentDefinitionRow, AgentDefinitionRow.agent_id == AgentRunRow.agent_id)
        .where(
            UsageEvent.user_id == user,
            AgentRunRow.owner_user_id == user,
            UsageEvent.timestamp >= now - REPORT_WINDOW,
        )
        .group_by(AgentRunRow.agent_id, AgentDefinitionRow.name)
        .order_by(func.coalesce(func.sum(UsageEvent.estimated_cost), 0.0).desc())
        .limit(MAX_AGENTS)
    )).all()

    limits = _limits(request)
    daily = limits.per_user_daily_cost_limit if limits else 0.0
    return UsageResponse(
        spent_24h=round(spent_24h, 6),
        daily_limit=daily,
        remaining_24h=round(max(0.0, daily - spent_24h), 6),
        calls_last_minute=calls,
        calls_per_minute_limit=limits.per_user_calls_per_minute if limits else 0,
        spent_30d=round(spent_30d, 6),
        agents_30d=[
            AgentSpend(agent_id=agent_id, name=name, runs=int(runs), spent=round(float(spent), 6))
            for agent_id, name, runs, spent in rows
        ],
    )
