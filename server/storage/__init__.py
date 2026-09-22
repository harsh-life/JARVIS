"""Persistence / storage abstraction (foundation §11).

`shared/schemas/` (Pydantic) defines Track B's data *contracts* and is
ORM-agnostic by design — it does not import SQLAlchemy or anything else
storage-specific. This package is the *one* place those contracts get a
concrete, replaceable storage backend.

`StorageBackend` is the explicit interface (§11: "storage backend must be
replaceable behind an explicit interface"); `SQLAlchemyStorageBackend` is
today's implementation, chosen for `STORE-004 [IMPL]` because it satisfies
every locked constraint 01_DATA_MODEL_SCHEMA.md §14 states (supports the
unique/partial indexes the schema needs, supports the visibility-filter
query pattern, never stores a secret value) while remaining swappable to
Postgres or another engine via `database_url` alone — no code change.

Foundation does NOT implement cross-user authorization inside this layer,
and does NOT claim physical cross-user isolation merely because rows live
in separate tables (that question is OD-A1, 14_SECURITY_BLAST_RADIUS.md —
explicitly out of scope here, per 01 §14 STORE-003).
"""

from server.storage import models  # noqa: F401  (registers ORM tables on Base.metadata)
from server.storage.backend import SQLAlchemyStorageBackend, StorageBackend
from server.storage.base import Base

__all__ = ["Base", "SQLAlchemyStorageBackend", "StorageBackend"]
