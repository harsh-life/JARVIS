"""SQLAlchemy declarative base.

Kept separate from `models.py` so Alembic's `env.py` can import just the
metadata without pulling in the rest of the storage package.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
