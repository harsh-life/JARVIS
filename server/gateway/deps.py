"""Dependency-injection scaffolding.

Foundation provides exactly one real dependency (`get_db_session`) because
that is all foundation has to inject — auth-derived dependencies
(current_user, current_device, current_session) belong to
03_AUTH_IDENTITY_SESSION.md and do not exist here. Nothing in this branch
introduces a fake/stub version of them (§8: "Do not create fake auth
middleware merely to 'get the API working'").
"""

from __future__ import annotations

from typing import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from server.storage import StorageBackend


def get_storage_backend(request: Request) -> StorageBackend:
    return request.app.state.storage


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    backend: StorageBackend = request.app.state.storage
    async with backend.session() as session:
        yield session
