"""audit ledger references are identifiers, not foreign keys (H-1, PostgreSQL)

The audit trail records refusals as well as successes, so a row can name an id
that does not exist: a probe of an unknown graph, a proof from an unknown
device, a request whose session was just revoked (02 §1.2 — the audit row must
survive the refusal). docs/DECISION_REGISTER.md §2B already records that these
rows "deliberately reference ids that may not exist" and that integrity is the
service layer's. SQLite never enforced the four foreign keys; PostgreSQL does,
and would reject the very audit row a refusal must leave. Only `audit_events`
changes: every other foreign key stays, and PostgreSQL now enforces it.

Revision ID: a2d6e8f4c0b9
Revises: c9e5a1f3b7d4
Create Date: 2026-09-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a2d6e8f4c0b9"
down_revision: Union[str, Sequence[str], None] = "c9e5a1f3b7d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_REFERENCES = (
    ("user_id", "users", "user_id"),
    ("device_id", "devices", "device_id"),
    ("session_id", "sessions", "session_id"),
    ("graph_id", "graphs", "graph_id"),
)


def _audit_table(*, foreign_keys: bool) -> sa.Table:
    """`audit_events` as the initial migration defines it, with or without the
    four foreign keys — the shape SQLite's batch mode rebuilds the table to."""

    fks = [sa.ForeignKeyConstraint([col], [f"{table}.{ref}"]) for col, table, ref in _REFERENCES] if foreign_keys else []
    table = sa.Table(
        "audit_events", sa.MetaData(),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("device_id", sa.Uuid(), nullable=True),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("graph_id", sa.Uuid(), nullable=True),
        sa.Column("actor", sa.Enum("user", "agent", "tool", "system", "superuser", name="audit_actor",
                                   native_enum=False), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("resource", sa.String(), nullable=False),
        sa.Column("decision", sa.Enum("allow", "deny", "require_confirmation", name="audit_decision",
                                      native_enum=False), nullable=True),
        sa.Column("result", sa.Enum("success", "failure", "blocked", name="audit_result", native_enum=False),
                  nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
        *fks,
    )
    sa.Index("ix_audit_events_graph_timestamp", table.c.graph_id, table.c.timestamp)
    sa.Index("ix_audit_events_request_id", table.c.request_id)
    sa.Index("ix_audit_events_user_timestamp", table.c.user_id, table.c.timestamp)
    return table


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("audit_events", recreate="always",
                                  copy_from=_audit_table(foreign_keys=False)):
            pass
        return
    for fk in sa.inspect(bind).get_foreign_keys("audit_events"):
        op.drop_constraint(fk["name"], "audit_events", type_="foreignkey")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("audit_events", recreate="always",
                                  copy_from=_audit_table(foreign_keys=True)):
            pass
        return
    # Restores the constraints; fails if the ledger already names an id that
    # does not exist — which is exactly what this revision allows.
    for col, table, ref in _REFERENCES:
        op.create_foreign_key(f"audit_events_{col}_fkey", "audit_events", table, [col], [ref])
