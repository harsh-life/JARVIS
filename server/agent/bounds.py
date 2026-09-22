"""Hard runtime ceilings (05 §3, RATE-001) and the concurrency gate (13 §2).

`[LOCKED]` every ceiling exists and is enforced by the runtime, never by the
model. The numbers are `[IMPL]` / OD-02 and come from configuration.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from server.agent.ports import UsageLimitReached
from shared.schemas.authorization import Principal


@dataclass(frozen=True)
class RuntimeBounds:
    max_iterations: int = 12
    max_model_calls: int = 16
    max_tool_calls: int = 24
    max_model_tool_nesting_depth: int = 1
    wall_clock_timeout_seconds: float = 120.0
    per_task_budget: float = 0.0
    max_parse_retries: int = 2
    max_input_chars: int = 8000
    max_observation_chars: int = 4000
    max_context_chars: int = 24000
    # Backstop lifetime of a task-scoped grant. The grant is also revoked at task
    # end; this bounds it if the process dies first.
    task_grant_ttl_seconds: float = 900.0


@dataclass(frozen=True)
class ConcurrencyLimits:
    per_session: int = 1
    per_user: int = 2
    global_: int = 8


class ConcurrencyGate:
    """In-process concurrent-task caps (per session, per user, global).

    Single-process pilot, so an in-memory counter is the ledger for *running*
    tasks. Check-and-increment happens with no `await` in between, so it is
    atomic under asyncio. A paused task holds no slot: it is not running.
    """

    def __init__(self, limits: ConcurrencyLimits) -> None:
        self._limits = limits
        self._by_session: dict[uuid.UUID, int] = {}
        self._by_user: dict[uuid.UUID, int] = {}
        self._global = 0

    @asynccontextmanager
    async def slot(self, principal: Principal) -> AsyncIterator[None]:
        session_count = self._by_session.get(principal.session_id, 0)
        user_count = self._by_user.get(principal.user_id, 0)
        if session_count >= self._limits.per_session:
            raise UsageLimitReached("per_session_concurrency", retry_after_seconds=5)
        if user_count >= self._limits.per_user:
            raise UsageLimitReached("per_user_concurrency", retry_after_seconds=5)
        if self._global >= self._limits.global_:
            raise UsageLimitReached("global_concurrency", retry_after_seconds=5)

        self._by_session[principal.session_id] = session_count + 1
        self._by_user[principal.user_id] = user_count + 1
        self._global += 1
        try:
            yield
        finally:
            self._by_session[principal.session_id] -= 1
            if self._by_session[principal.session_id] <= 0:
                del self._by_session[principal.session_id]
            self._by_user[principal.user_id] -= 1
            if self._by_user[principal.user_id] <= 0:
                del self._by_user[principal.user_id]
            self._global -= 1
