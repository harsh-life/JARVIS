"""Which relational store a test runs on (H-1).

The pilot's runtime store is PostgreSQL (docs/DECISION_REGISTER.md H-1). Set

    HYPERMIND_TEST_DATABASE_URL=postgresql+asyncpg://<user>@<host>:<port>/<admin-db>

to run the suites on it: every call to `database_url_for` then creates a fresh,
empty PostgreSQL database for that test and returns its URL. The URL names a
server the tests may create and drop databases on — never a deployment's
database — and carries no password (asyncpg reads `PGPASSWORD` / `~/.pgpass`).

Unset, every call returns the throwaway SQLite file it always did, so the suite
still runs anywhere with no database server. The PRD #32 acceptance refuses to
run on SQLite (`tests/memory/test_prd32_service_measurement.py`).

The same path always maps to the same database within one run, so a restart
test that opens a second backend "on the same file" opens the same database.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy.engine import make_url

TEST_DATABASE_ENV = "HYPERMIND_TEST_DATABASE_URL"
_PREFIX = "hm_test_"
_created: dict[str, str] = {}


def admin_url() -> str | None:
    return os.environ.get(TEST_DATABASE_ENV) or None


def _asyncpg_dsn(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


def _run(coro) -> None:
    # Called from sync and async code alike; a private thread keeps it off the
    # test's own event loop.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, coro).result()


async def _execute(admin: str, *statements: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(_asyncpg_dsn(admin))
    try:
        for statement in statements:
            await conn.execute(statement)
    finally:
        await conn.close()


def database_url_for(path: Path | str) -> str:
    """The database URL a test should use for `path` (a throwaway location)."""

    admin = admin_url()
    if admin is None:
        return f"sqlite+aiosqlite:///{path}"
    key = str(path)
    if key not in _created:
        name = _PREFIX + hashlib.sha256(key.encode()).hexdigest()[:24]
        _run(_execute(admin, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)', f'CREATE DATABASE "{name}"'))
        _created[key] = make_url(admin).set(database=name).render_as_string(hide_password=False)
    return _created[key]


def created_keys() -> set[str]:
    return set(_created)


def drop_created_databases(keep: set[str] | None = None) -> None:
    """Drop the databases created since `keep` was taken (all of them if None)."""

    admin = admin_url()
    doomed = [k for k in _created if keep is None or k not in keep]
    if admin is None or not doomed:
        return
    names = [make_url(_created.pop(k)).database for k in doomed]
    _run(_execute(admin, *(f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)' for n in names)))


def backend_name() -> str:
    return "postgresql" if admin_url() else "sqlite"


async def table_names(storage) -> list[str]:
    from sqlalchemy import inspect

    async with storage.engine.connect() as conn:
        return await conn.run_sync(lambda sync: inspect(sync).get_table_names())


async def store_contents(storage) -> bytes:
    """Everything the relational store holds at rest, as bytes — for tests that
    look for a value that must (or may) be there: the database file itself on
    SQLite; every row of every table on PostgreSQL, whose files are the server's."""

    from sqlalchemy import text

    url = storage.engine.url
    if url.get_backend_name() == "sqlite":
        return Path(url.database).read_bytes()
    dump = []
    async with storage.engine.connect() as conn:
        for table in await table_names(storage):
            dump.append(repr((await conn.execute(text(f'SELECT * FROM "{table}"'))).all()))
    return "\n".join(dump).encode()
