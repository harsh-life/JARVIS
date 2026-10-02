"""The runtime's UsagePort, satisfied by the usage ledger (13)."""

from __future__ import annotations

import uuid
from collections import deque
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from server.agent.ports import UsageLimitReached
from server.security.usage import LimitExceeded, UsagePolicy
from shared.schemas.authorization import Principal, device_of, session_of
from shared.schemas.enums import UsageKind


class RuntimeUsageAdapter:
    """H-1: each admitted call is held by the policy until its ledger row is
    visible to every other request — the commit of the transaction that wrote
    it — and let go when that transaction is rolled back instead (the row is
    gone, and so is the call's request). A call admitted but not yet recorded
    survives the commits `holding_admissions` wraps (the runtime releases the
    store *before* the call it just admitted), and is let go at any other
    transaction end: by then the runtime is done with it."""

    def __init__(self, *, policy: UsagePolicy, session: AsyncSession, request_id: uuid.UUID) -> None:
        self._policy = policy
        self._session = session
        self._request_id = request_id
        self._admitted: deque[int] = deque()
        self._recorded: list[int] = []
        self._holding = False
        event.listen(session.sync_session, "after_commit", self._committed)
        event.listen(session.sync_session, "after_rollback", self._rolled_back)

    @contextmanager
    def holding_admissions(self) -> Iterator[None]:
        self._holding = True
        try:
            yield
        finally:
            self._holding = False

    def _committed(self, _session) -> None:
        for admission_id in self._recorded:
            self._policy.release(admission_id)
        self._recorded.clear()
        if not self._holding:
            self._release_admitted()

    def _rolled_back(self, _session) -> None:
        self._committed(_session)
        self._release_admitted()

    def _release_admitted(self) -> None:
        while self._admitted:
            self._policy.release(self._admitted.popleft())

    async def precheck(self, *, principal: Principal, projected_cost: float) -> None:
        try:
            admission_id = await self._policy.admit(
                self._session,
                user_id=principal.user_id,
                device_id=device_of(principal),
                projected_cost=projected_cost,
            )
        except LimitExceeded as exc:
            raise UsageLimitReached(exc.limit, retry_after_seconds=exc.retry_after_seconds) from None
        self._admitted.append(admission_id)

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
    ) -> uuid.UUID:
        usage_id = await self._policy.ledger.record(
            self._session,
            request_id=self._request_id,
            user_id=principal.user_id,
            device_id=device_of(principal),
            session_id=session_of(principal),
            graph_id=graph_id,
            kind=kind,
            units=units,
            estimated_cost=estimated_cost,
            provider=provider,
            model=model,
            tool_id=tool_id,
        )
        # The runtime records each call after the one precheck that admitted it.
        if self._admitted:
            admission_id = self._admitted.popleft()
            self._policy.recorded(admission_id, self._session.sync_session)
            self._recorded.append(admission_id)
        return usage_id
