"""Shared fixtures for the foundation test suite.

Each test gets its own throwaway SQLite file, created via
`StorageBackend.init_models()` and destroyed afterward — "test
databases/fixtures must be easy to create and destroy" (§11).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from server.storage import SQLAlchemyStorageBackend


@pytest_asyncio.fixture
async def storage(tmp_path) -> AsyncIterator[SQLAlchemyStorageBackend]:
    db_path = tmp_path / f"test_{uuid.uuid4().hex}.db"
    backend = SQLAlchemyStorageBackend(f"sqlite+aiosqlite:///{db_path}")
    await backend.init_models()
    try:
        yield backend
    finally:
        await backend.dispose()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
