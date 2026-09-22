"""The runtime's UsagePort, satisfied by the usage ledger (13)."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from server.agent.ports import UsageLimitReached
from server.security.usage import LimitExceeded, UsagePolicy
from shared.schemas.authorization import Principal
from shared.schemas.enums import UsageKind


class RuntimeUsageAdapter:
    def __init__(self, *, policy: UsagePolicy, session: AsyncSession, request_id: uuid.UUID) -> None:
        self._policy = policy
        self._session = session
        self._request_id = request_id

    async def precheck(self, *, principal: Principal, projected_cost: float) -> None:
        try:
            await self._policy.precheck(
                self._session,
                user_id=principal.user_id,
                device_id=principal.device_id,
                projected_cost=projected_cost,
            )
        except LimitExceeded as exc:
            raise UsageLimitReached(exc.limit, retry_after_seconds=exc.retry_after_seconds) from None

    async def record(
        self,
        *,
        principal: Principal,
        graph_id: uuid.UUID | None,
        kind: UsageKind,
        units: int,
        estimated_cost: float,
        provider: str | None = None,
        model: str | None = None,
        tool_id: str | None = None,
    ) -> None:
        await self._policy.ledger.record(
            self._session,
            request_id=self._request_id,
            user_id=principal.user_id,
            device_id=principal.device_id,
            session_id=principal.session_id,
            graph_id=graph_id,
            kind=kind,
            units=units,
            estimated_cost=estimated_cost,
            provider=provider,
            model=model,
            tool_id=tool_id,
        )
