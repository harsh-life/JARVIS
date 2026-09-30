"""What the HTTP layer needs from the Agent Factory (docs/29 §23.2).

`server.gateway` sits below `server.agents` in the layering (16 §2), so it
declares this Protocol and the composition root supplies the implementation
(`server/composition/agents.py`) on `app.state.agent_factory` — the same
pattern as `scheduler_port.py`. Without one (`agents.enabled: false`), every
endpoint answers `503 dependency_unavailable` with `agents`.

Every method takes the authenticated `Principal`; none accepts an owner, a
graph, a capability or a spec from a request body. Implementations raise
`AppError` in 02 §1.7's vocabulary.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from server.security.audit import AuditLogger
from shared.schemas.agent_factory import (
    AgentDetail,
    AgentListResponse,
    AgentView,
    CompiledAgentSpecView,
    CompileOutcome,
    CreateAgentRequest,
)
from shared.schemas.authorization import Principal


class AgentFactoryPort(Protocol):
    async def compile(
        self, session: AsyncSession, *, principal: Principal, draft: Any, agent_id: uuid.UUID | None,
        audit: AuditLogger,
    ) -> CompileOutcome: ...

    async def preview(
        self, session: AsyncSession, *, principal: Principal, compile_id: uuid.UUID, audit: AuditLogger
    ) -> CompiledAgentSpecView: ...

    async def create(
        self, session: AsyncSession, *, principal: Principal, body: CreateAgentRequest,
        confirmation_token: str | None, audit: AuditLogger,
    ) -> AgentView: ...

    async def list(self, session: AsyncSession, *, principal: Principal, audit: AuditLogger) -> AgentListResponse: ...

    async def get(
        self, session: AsyncSession, *, principal: Principal, agent_id: uuid.UUID, audit: AuditLogger
    ) -> AgentDetail: ...

    async def update(
        self, session: AsyncSession, *, principal: Principal, agent_id: uuid.UUID, body: CreateAgentRequest,
        confirmation_token: str | None, audit: AuditLogger,
    ) -> AgentView: ...

    async def delete(
        self, session: AsyncSession, *, principal: Principal, agent_id: uuid.UUID,
        confirmation_token: str | None, audit: AuditLogger,
    ) -> None: ...


__all__ = ["AgentFactoryPort"]
