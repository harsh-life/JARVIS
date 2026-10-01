"""agent factory phase 3: run tokens and the gateway nonce ledger

Revision ID: d4f6a8c0e2b5
Revises: c7e9b1d3f5a8
Create Date: 2026-10-01 21:00:00.000000

docs/29 §11.3 (`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]`). Adds
`agent_run_tokens` (SHA-256 of each run token, bound to its run, agent, spec
hash and gateway) and `agent_gateway_nonces` (each request nonce a token used,
for replay refusal and idempotent retries). Additive; nothing writes to either
unless `agents.enabled`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d4f6a8c0e2b5"
down_revision: Union[str, Sequence[str], None] = "c7e9b1d3f5a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_run_tokens",
        sa.Column("token_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("spec_hash", sa.String(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(), nullable=True),
        sa.CheckConstraint("purpose IN ('model','tool')", name="ck_agent_run_tokens_purpose"),
        sa.ForeignKeyConstraint(["agent_id"], ["agent_definitions.agent_id"]),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"]),
        sa.PrimaryKeyConstraint("token_id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_agent_run_tokens_run", "agent_run_tokens", ["run_id"])
    op.create_index("ix_agent_run_tokens_agent", "agent_run_tokens", ["agent_id"])
    op.create_index("ix_agent_run_tokens_expires_at", "agent_run_tokens", ["expires_at"])
    op.create_table(
        "agent_gateway_nonces",
        sa.Column("token_id", sa.Uuid(), nullable=False),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("response", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('in_progress','done')", name="ck_agent_gateway_nonces_status"),
        sa.ForeignKeyConstraint(["token_id"], ["agent_run_tokens.token_id"]),
        sa.PrimaryKeyConstraint("token_id", "nonce"),
    )


def downgrade() -> None:
    op.drop_table("agent_gateway_nonces")
    op.drop_index("ix_agent_run_tokens_expires_at", table_name="agent_run_tokens")
    op.drop_index("ix_agent_run_tokens_agent", table_name="agent_run_tokens")
    op.drop_index("ix_agent_run_tokens_run", table_name="agent_run_tokens")
    op.drop_table("agent_run_tokens")
