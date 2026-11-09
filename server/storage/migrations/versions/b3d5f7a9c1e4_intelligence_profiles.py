"""intelligence profiles: user-owned, versioned experiment profiles (docs/30)

Revision ID: b3d5f7a9c1e4
Revises: a9c1e3f5b7d0
Create Date: 2026-11-06 12:00:00.000000

docs/30 (Phase 0, Intelligence Workbench): a user's named experiment profiles
and their immutable versions — the validated `IntelligenceProfileDocument` and
its canonical SHA-256. Owner-scoped (one private workspace per user; no graph
scope). No secret, endpoint or price is stored: a document names operator
catalogue entries by id only. Nothing in the production request lifecycle
reads these tables.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b3d5f7a9c1e4"
down_revision: Union[str, Sequence[str], None] = "a9c1e3f5b7d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "intelligence_profiles",
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("current_version >= 1", name="ck_intelligence_profiles_version"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("profile_id"),
    )
    op.create_index("ix_intelligence_profiles_owner", "intelligence_profiles", ["owner_user_id"])
    op.create_table(
        "intelligence_profile_versions",
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_intelligence_profile_versions_version"),
        sa.ForeignKeyConstraint(["profile_id"], ["intelligence_profiles.profile_id"]),
        sa.PrimaryKeyConstraint("profile_id", "version"),
    )


def downgrade() -> None:
    op.drop_table("intelligence_profile_versions")
    op.drop_index("ix_intelligence_profiles_owner", table_name="intelligence_profiles")
    op.drop_table("intelligence_profiles")
