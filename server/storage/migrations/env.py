import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Import our own metadata + config loader so migrations always target the
# same schema/URL as the running application — never a second, drifting
# definition (§11: "initialization must be deterministic").
from server.storage.base import Base
from server.storage import models  # noqa: F401  (registers tables on Base.metadata)
from server.config import ConfigError, load_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_database_url() -> str:
    """Resolution order: HYPERMIND_DATABASE_URL env var (CI/tests) ->
    server/config's config.yaml -> the placeholder in alembic.ini (never
    actually usable, so this always raises before reaching it in practice).
    """

    env_override = os.environ.get("HYPERMIND_DATABASE_URL")
    if env_override:
        return env_override

    try:
        app_config = load_config()
    except ConfigError:
        return config.get_main_option("sqlalchemy.url")
    return app_config.database_url


def run_migrations_offline() -> None:
    url = _resolve_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _resolve_database_url()
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
