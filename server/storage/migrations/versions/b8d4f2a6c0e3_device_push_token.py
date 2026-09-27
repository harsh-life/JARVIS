"""devices: push wake registration (docs/23 §4)

Revision ID: b8d4f2a6c0e3
Revises: a7c3e9f1b5d2
Create Date: 2026-09-27 12:30:00.000000

Adds to `devices`:
* `push_provider` / `push_token` — the device's own push registration
  (FCM), set by the device over its authenticated session. Unique: a token
  wakes exactly one device.
* `push_token_registered_at` — when it was bound.

Additive and nullable: push is off by default, and a device without a token is
simply never woken (it reconnects when opened or when its network returns).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b8d4f2a6c0e3"
down_revision: Union[str, Sequence[str], None] = "a7c3e9f1b5d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("devices") as batch:
        batch.add_column(sa.Column("push_provider", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("push_token", sa.String(length=4096), nullable=True))
        batch.add_column(sa.Column("push_token_registered_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ux_devices_push_token", ["push_token"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("devices") as batch:
        batch.drop_index("ux_devices_push_token")
        batch.drop_column("push_token_registered_at")
        batch.drop_column("push_token")
        batch.drop_column("push_provider")
