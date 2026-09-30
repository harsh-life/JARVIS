"""Fixtures for the Agent Factory suites: a throwaway store per test (SQLite,
or PostgreSQL with `HYPERMIND_TEST_DATABASE_URL`)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest_asyncio

from server.storage import SQLAlchemyStorageBackend
from server.storage.models import User
from shared.schemas.enums import UserStatus
from tests.dbsupport import database_url_for

NOW = datetime(2026, 9, 30, 6, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def storage(tmp_path) -> AsyncIterator[SQLAlchemyStorageBackend]:
    backend = SQLAlchemyStorageBackend(database_url_for(tmp_path / f"agents_{uuid.uuid4().hex}.db"))
    await backend.init_models()
    try:
        yield backend
    finally:
        await backend.dispose()


async def make_user(storage: SQLAlchemyStorageBackend) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with storage.session() as session:
        session.add(User(user_id=user_id, oidc_subject=f"sub-{user_id.hex}",
                         oidc_issuer="https://accounts.google.com", status=UserStatus.ACTIVE, created_at=NOW))
        await session.commit()
    return user_id

