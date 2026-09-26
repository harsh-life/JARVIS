"""auth: app-state nonce for the Android App Link login handoff

Revision ID: e4f7a2c9b1d3
Revises: d8e2b6c4a0f1
Create Date: 2026-09-26 19:00:00.000000

Adds `oidc_login_states.app_state` — a nonce the Android app generates before
opening the browser and checks when the App Link returns the bootstrap token,
so an app accepts only the login it started (docs/23 §3). Nullable: a plain
JSON login has none.

Additive: existing rows get NULL.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e4f7a2c9b1d3"
down_revision: Union[str, Sequence[str], None] = "d8e2b6c4a0f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("oidc_login_states") as batch:
        batch.add_column(sa.Column("app_state", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("oidc_login_states") as batch:
        batch.drop_column("app_state")
