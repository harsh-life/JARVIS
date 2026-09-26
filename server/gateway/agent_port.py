"""What the HTTP layer needs from the agent runtime.

`server.gateway` sits below `server.agent` in the layering (16 §2), so it cannot
import the runtime. It declares this Protocol instead; the composition root
(`server/composition/`) supplies an implementation on `app.state.agent_tasks`.

Every method takes the authenticated `Principal` resolved from the bearer token
by `server/gateway/deps.py` — never an identity from the request body
(PHONE-003).
"""

from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from server.security.audit import AuditLogger
from shared.schemas.agent import AgentResult, TaskMode, ToolSummary
from shared.schemas.authorization import Principal


class AgentTaskPort(Protocol):
    async def submit(
        self, session: AsyncSession, *, principal: Principal, user_input: str, audit: AuditLogger,
        mode: TaskMode = TaskMode.EXECUTE,
    ) -> AgentResult: ...

    async def confirm(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        task_id: uuid.UUID,
        confirmation_token: str,
        approve: bool,
        step_up_fresh: bool,
        audit: AuditLogger,
    ) -> AgentResult: ...

    async def cancel(
        self, session: AsyncSession, *, principal: Principal, task_id: uuid.UUID, audit: AuditLogger
    ) -> AgentResult: ...

    async def get(
        self, session: AsyncSession, *, principal: Principal, task_id: uuid.UUID, audit: AuditLogger
    ) -> AgentResult: ...

    def tool_summaries(self) -> list[ToolSummary]: ...

    async def reconcile_after_restart(self, session: AsyncSession, *, audit: AuditLogger) -> list[uuid.UUID]:
        """Close every task a previous server run left unfinished; startup only."""
        ...
