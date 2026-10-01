"""What the operator console routes need (28 §3) — a read-only Protocol.

`server.gateway` sits below `server.dashboard` in the layering (16 §2), so it
declares this interface and the composition root supplies the implementation
(`server.dashboard.console.OperatorConsole`) on `app.state.operator_console`.
Every method is a read that returns plain data; none of them changes anything.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession


class OperatorConsolePort(Protocol):
    async def banners(self, session: AsyncSession) -> list[dict]: ...

    async def health(self, session: AsyncSession, *, probe_models: bool = False) -> dict: ...

    async def tasks(self, session: AsyncSession, *, limit: int = 100, status: str | None = None) -> dict: ...

    async def recovery(self, session: AsyncSession, *, limit: int = 100) -> dict: ...

    async def break_glass(self, session: AsyncSession, *, limit: int = 100) -> dict: ...

    async def evaluations(self, session: AsyncSession, *, limit: int = 100) -> dict: ...

    async def usage(self, session: AsyncSession) -> dict: ...

    async def memory(self, session: AsyncSession) -> dict: ...

    async def devices(self, session: AsyncSession, *, limit: int = 200) -> dict: ...

    async def audit(self, session: AsyncSession, *, action: str | None = None, user_id: uuid.UUID | None = None,
                    result: str | None = None, resource_prefix: str | None = None,
                    since: datetime | None = None, limit: int = 200) -> dict: ...

    async def configuration(self, session: AsyncSession) -> dict: ...

    async def task_content(self, session: AsyncSession, task_id: uuid.UUID) -> dict | None: ...

    async def agents(self, session: AsyncSession, *, limit: int = 100) -> dict: ...
