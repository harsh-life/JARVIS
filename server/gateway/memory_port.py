"""What the HTTP layer needs from persistent memory and the Knowledge Vault.

`server.gateway` sits below `server.memory` and `server.vault` in the layering
(16 §2), so it declares these Protocols and the composition root supplies the
implementations (`server/composition/memory.py`) on `app.state.memory` and
`app.state.vault` — the same pattern as `agent_port.py`.

Every method takes the authenticated `Principal` resolved from the bearer token
(PHONE-003); none accepts an owner, a source user, or a visibility claim from a
request body as authority. Implementations raise `AppError` in 02 §1.7's
vocabulary, including `503 dependency_unavailable` with `mem0` / `vault`.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from server.security.audit import AuditLogger
from shared.schemas.authorization import Principal
from shared.schemas.memory import (
    Mem0Fact,
    MemoryCreateRequest,
    MemoryListResponse,
    MemoryPatchRequest,
    VaultQueryRequest,
    VaultQueryResponse,
)


class MemoryPort(Protocol):
    async def list(
        self, session: AsyncSession, *, principal: Principal, query: str | None, limit: int, audit: AuditLogger
    ) -> MemoryListResponse: ...

    async def add(
        self, session: AsyncSession, *, principal: Principal, body: MemoryCreateRequest, audit: AuditLogger
    ) -> Mem0Fact: ...

    async def recall(
        self, session: AsyncSession, *, principal: Principal, fact_id: uuid.UUID, audit: AuditLogger
    ) -> Mem0Fact:
        """One fact, if the engine lets the caller read it (an idempotent replay)."""
        ...

    async def patch(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        fact_id: uuid.UUID,
        body: MemoryPatchRequest,
        confirmation_token: str | None,
        audit: AuditLogger,
    ) -> Mem0Fact: ...

    async def delete(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        fact_id: uuid.UUID,
        confirmation_token: str | None,
        audit: AuditLogger,
    ) -> None: ...

    async def status(self) -> dict: ...


class VaultPort(Protocol):
    async def query(
        self, session: AsyncSession, *, principal: Principal, request: VaultQueryRequest
    ) -> VaultQueryResponse: ...

    async def status(self) -> dict: ...


__all__ = ["MemoryPort", "VaultPort"]
