"""presentation settings: one row per user (pilot plan C10)

Revision ID: a9c1e3f5b7d0
Revises: d1f3b5a7c9e2
Create Date: 2026-10-13 14:00:00.000000

Pilot plan C10 (step M2; OD-CHAR-1/2/3, register §2M and §2N): a user's
character presentation settings — the validated `CharacterPresentationSettings`
document (visibility, profile, transition, reduced motion, thresholds,
confidence floor, timing). Presentation only; no free text; nothing on the
server decides anything from it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a9c1e3f5b7d0"
down_revision: Union[str, Sequence[str], None] = "d1f3b5a7c9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "presentation_settings",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("presentation_settings")
