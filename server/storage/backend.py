"""The storage backend interface + its SQLAlchemy implementation.

§11 requirement: "storage backend must be replaceable behind an explicit
interface." `StorageBackend` is that interface; nothing outside this module
should construct a SQLAlchemy engine/session directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from server.storage.base import Base


class StorageBackend(ABC):
    """Explicit interface a storage backend must satisfy. A future
    non-SQLAlchemy backend (or a different SQLAlchemy dialect) implements
    this same contract; nothing above this layer depends on SQLAlchemy
    directly."""

    @abstractmethod
    def session(self) -> AsyncIterator[AsyncSession]:
        """An async context manager yielding one unit-of-work session."""

    @abstractmethod
    async def init_models(self) -> None:
        """Create tables if they don't exist yet.

        For real deployments, migrations (server/storage/migrations, Alembic)
        are the reproducible path (§11: "migrations must be reproducible").
        This method exists for tests and quick local bootstrapping, where a
        throwaway SQLite file is created and destroyed per test — see
        tests/foundation/conftest.py.
        """

    @abstractmethod
    async def dispose(self) -> None:
        """Release all connections/resources."""


class SQLAlchemyStorageBackend(StorageBackend):
    """STORE-004 [IMPL]: SQLAlchemy async engine, chosen for the reasons in
    server/storage/__init__.py's docstring. `database_url` is any
    SQLAlchemy-compatible async URL — swapping SQLite for Postgres is a
    config change (`database_url` in server/config), not a code change.
    """

    def __init__(self, database_url: str, *, echo: bool = False) -> None:
        self._database_url = database_url
        self._engine: AsyncEngine = create_async_engine(database_url, echo=echo)
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as session:
            yield session

    async def init_models(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self._engine.dispose()
