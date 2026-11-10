"""auth: the desktop companion's device identity and loopback login handoff

Revision ID: c7e9a1b3d5f8
Revises: b3d5f7a9c1e4
Create Date: 2026-11-10 18:00:00.000000

OD-DESK-1 (`docs/DECISION_REGISTER.md` §2N): the desktop companion is a
distinct registered device platform, `desktop`, signed in through the system
browser and a loopback handoff (`docs/CHARACTER_DESKTOP_PROPOSAL.md` §8).

* `oidc_login_states.desktop_state` / `desktop_challenge` / `desktop_port` —
  the companion's own nonce, the S256 challenge of the PKCE verifier only the
  companion holds, and the loopback port the callback returns to. Nullable:
  every other login has none.
* `desktop_handoffs` — the one-time code returned to the loopback listener in
  place of the bootstrap token, stored as a hash, single-use, short TTL.
* `bootstrap_tokens.platform` — the platform a token may register when it is
  bound to one (a handoff's token registers only a `desktop` device).

`devices.platform` needs no change: it is a non-native enum (VARCHAR(7), no
CHECK constraint), and `desktop` fits it as `android` does.

Additive: existing rows get NULL; nothing is backfilled.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c7e9a1b3d5f8"
down_revision: Union[str, Sequence[str], None] = "b3d5f7a9c1e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("oidc_login_states") as batch:
        batch.add_column(sa.Column("desktop_state", sa.String(), nullable=True))
        batch.add_column(sa.Column("desktop_challenge", sa.String(), nullable=True))
        batch.add_column(sa.Column("desktop_port", sa.Integer(), nullable=True))

    with op.batch_alter_table("bootstrap_tokens") as batch:
        batch.add_column(
            sa.Column(
                "platform",
                sa.Enum("android", "desktop", name="bootstrap_platform", native_enum=False),
                nullable=True,
            )
        )

    op.create_table(
        "desktop_handoffs",
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("code_challenge", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("code_hash"),
    )


def downgrade() -> None:
    op.drop_table("desktop_handoffs")
    with op.batch_alter_table("bootstrap_tokens") as batch:
        batch.drop_column("platform")
    with op.batch_alter_table("oidc_login_states") as batch:
        batch.drop_column("desktop_port")
        batch.drop_column("desktop_challenge")
        batch.drop_column("desktop_state")
