"""auth: device step-up key and re-attestation (docs/23 §3, 03 §5.5)

Revision ID: a7c3e9f1b5d2
Revises: f5a1c7e3d9b2
Create Date: 2026-09-27 06:30:00.000000

Adds to `devices`:
* `step_up_public_key` — SPKI (P-256) of a key that needs the user's
  biometric or device credential for every use; public, not a secret.
* `step_up_challenge_hash` / `step_up_challenge_expires_at` — the one
  outstanding single-use challenge (hash only).
* `reattested_at` — when the device last proved user presence with it.

Additive and nullable: existing devices have no step-up key, so approving a
`high_irreversible` action needs one registered first (re-enrolment).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a7c3e9f1b5d2"
down_revision: Union[str, Sequence[str], None] = "f5a1c7e3d9b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("devices") as batch:
        batch.add_column(sa.Column("step_up_public_key", sa.String(), nullable=True))
        batch.add_column(sa.Column("step_up_challenge_hash", sa.String(), nullable=True))
        batch.add_column(sa.Column("step_up_challenge_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("reattested_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("devices") as batch:
        batch.drop_column("reattested_at")
        batch.drop_column("step_up_challenge_expires_at")
        batch.drop_column("step_up_challenge_hash")
        batch.drop_column("step_up_public_key")
