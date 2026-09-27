"""Fixtures for the scheduler suite (docs/22).

A throwaway SQLite file per test, created through the same `init_models()` the
other suites use. `db_path` is exposed so the restart tests can open a *second*
storage backend on the same file — a real process restart as far as the
database is concerned.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from server.storage import SQLAlchemyStorageBackend
from server.storage.models import User
from shared.schemas.enums import UserStatus

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / f"sched_{uuid.uuid4().hex}.db"


@pytest_asyncio.fixture
async def storage(db_path) -> AsyncIterator[SQLAlchemyStorageBackend]:
    backend = SQLAlchemyStorageBackend(f"sqlite+aiosqlite:///{db_path}")
    await backend.init_models()
    try:
        yield backend
    finally:
        await backend.dispose()


async def make_user(storage: SQLAlchemyStorageBackend, *, status: UserStatus = UserStatus.ACTIVE) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with storage.session() as session:
        session.add(
            User(
                user_id=user_id,
                oidc_subject=f"sub-{user_id.hex}",
                oidc_issuer="https://accounts.google.com",
                status=status,
                created_at=NOW,
            )
        )
        await session.commit()
    return user_id


# The production composition root with a scripted model (tests/runtime), with
# the scheduler's agent tool registered alongside the harness's fake tools.
from tests.runtime.conftest import make_harness as _runtime_harness  # noqa: E402,F401


@pytest_asyncio.fixture
async def make_harness(_runtime_harness):
    async def _make(**kwargs):
        kwargs.setdefault("scheduler_tool", True)
        return await _runtime_harness(**kwargs)

    return _make


@pytest_asyncio.fixture
async def h(make_harness):
    return await make_harness()
