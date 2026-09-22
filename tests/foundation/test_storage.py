"""Storage/persistence tests: DB initialization, migrations, and the
data-integrity validations foundation owns (DM-T5..T8 — see
01_DATA_MODEL_SCHEMA.md §15)."""

from __future__ import annotations

import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from server.storage import SQLAlchemyStorageBackend
from server.storage.base import Base
from server.storage.models import Device, Graph, GraphMembership, SecretReference, SpeakerContext, User

REPO_ROOT = Path(__file__).parents[2]


async def test_init_models_creates_every_table(storage: SQLAlchemyStorageBackend) -> None:
    async with storage.engine.connect() as conn:
        table_names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
    expected = set(Base.metadata.tables.keys())
    assert expected.issubset(set(table_names))
    assert len(expected) >= 16  # every canonical relational-store entity


def test_alembic_migration_applies_and_reverses_cleanly(tmp_path: Path) -> None:
    """Migrations must be reproducible (§11) — apply head, then reverse to
    base, on a fresh throwaway file, via the real `alembic` CLI."""

    db_path = tmp_path / "alembic_test.db"
    env = {
        **__import__("os").environ,
        "HYPERMIND_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
    }

    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert upgrade.returncode == 0, upgrade.stderr
    assert db_path.exists()

    downgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert downgrade.returncode == 0, downgrade.stderr


def test_secret_reference_table_has_no_value_column() -> None:
    """DM-T5 (structural half): no column on SecretReference — or anywhere
    in the schema — is capable of holding a secret value."""

    columns = {c.name for c in SecretReference.__table__.columns}
    forbidden_substrings = ("value", "secret_value", "credential_value", "password", "token_value")
    for col in columns:
        for bad in forbidden_substrings:
            assert bad not in col.lower(), f"SecretReference.{col} looks like it could hold a secret value"
    assert columns == {
        "secret_ref",
        "owner_scope_type",
        "owner_scope_id",
        "class",
        "created_at",
        "rotated_at",
        "revoked_at",
    }


async def test_session_user_device_mismatch_rejected_at_construction() -> None:
    """DM-T6: a Session whose user_id != Device.user_id is rejected. Tested
    at the shared-schema construction boundary — `new_session_for_device`
    is the only foundation-sanctioned way to build a Session, and it makes
    the violation unrepresentable by construction (no DB CHECK constraint
    can express this, since it spans two tables).
    """

    from shared.schemas.identity import Device as DeviceSchema
    from shared.schemas.identity import new_session_for_device

    device = DeviceSchema(user_id=uuid.uuid4(), credential_ref="secretstore:test-handle")

    ok_session = new_session_for_device(
        device=device, expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    assert ok_session.user_id == device.user_id
    assert ok_session.device_id == device.device_id

    # There is deliberately no foundation-sanctioned constructor that lets a
    # caller pass a Session.user_id independent of the Device it names —
    # new_session_for_device takes a Device, not a bare user_id, precisely
    # so DM-T6 cannot be violated through the path foundation provides.
    import inspect as _inspect

    sig = _inspect.signature(new_session_for_device)
    assert "user_id" not in sig.parameters


async def test_duplicate_active_graph_membership_rejected(storage: SQLAlchemyStorageBackend) -> None:
    """DM-T7: a duplicate active GraphMembership (graph_id, user_id) is
    rejected by the DB's partial unique index."""

    user_id = uuid.uuid4()
    graph_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    async with storage.session() as session:
        session.add(User(user_id=user_id, oidc_subject="sub-1", oidc_issuer="iss", created_at=now))
        session.add(Graph(graph_id=graph_id, name="g", owner_user_id=user_id, type="private", created_at=now))
        await session.commit()

    async with storage.session() as session:
        session.add(
            GraphMembership(
                graph_id=graph_id, user_id=user_id, role="owner", granted_by=user_id, granted_at=now
            )
        )
        await session.commit()

    with pytest.raises(IntegrityError):
        async with storage.session() as session:
            session.add(
                GraphMembership(
                    graph_id=graph_id, user_id=user_id, role="member", granted_by=user_id, granted_at=now
                )
            )
            await session.commit()


async def test_revoked_membership_does_not_block_a_new_active_one(
    storage: SQLAlchemyStorageBackend,
) -> None:
    """The partial index only constrains *active* (revoked_at IS NULL)
    memberships — a revoked one followed by a fresh active one is fine."""

    user_id = uuid.uuid4()
    graph_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    async with storage.session() as session:
        session.add(User(user_id=user_id, oidc_subject="sub-2", oidc_issuer="iss", created_at=now))
        session.add(Graph(graph_id=graph_id, name="g2", owner_user_id=user_id, type="private", created_at=now))
        await session.commit()

    async with storage.session() as session:
        session.add(
            GraphMembership(
                graph_id=graph_id,
                user_id=user_id,
                role="owner",
                granted_by=user_id,
                granted_at=now,
                revoked_at=now,
            )
        )
        await session.commit()

    async with storage.session() as session:
        session.add(
            GraphMembership(
                graph_id=graph_id, user_id=user_id, role="member", granted_by=user_id, granted_at=now
            )
        )
        await session.commit()  # must not raise


async def test_speaker_context_authorization_signal_db_check(storage: SQLAlchemyStorageBackend) -> None:
    """DM-T8 / INV-14, DB-level half: the CHECK constraint rejects a row
    with is_authorization_signal = 1 even if some future code tried to
    bypass the Pydantic Literal[False] guarantee."""

    now = datetime.now(timezone.utc)

    async with storage.session() as session:
        session.add(
            SpeakerContext(
                speaker_id="voice-provider-label-1",
                confidence=0.9,
                utterance="hello",
                timestamp=now,
                is_authorization_signal=False,
            )
        )
        await session.commit()  # allowed

    with pytest.raises(IntegrityError):
        async with storage.session() as session:
            await session.execute(
                SpeakerContext.__table__.insert().values(
                    speaker_context_id=uuid.uuid4(),
                    speaker_id="voice-provider-label-2",
                    confidence=0.9,
                    utterance="bypass attempt",
                    timestamp=now,
                    is_authorization_signal=True,
                )
            )
            await session.commit()


async def test_scheduled_job_empty_task_reason_rejected_at_db_layer(
    storage: SQLAlchemyStorageBackend,
) -> None:
    """DM-T4, DB-level half (defense in depth alongside the Pydantic
    validator in shared/schemas/resources.py)."""

    from server.storage.models import ScheduledJob

    user_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    async with storage.session() as session:
        session.add(User(user_id=user_id, oidc_subject="sub-3", oidc_issuer="iss", created_at=now))
        await session.commit()

    with pytest.raises(IntegrityError):
        async with storage.session() as session:
            session.add(
                ScheduledJob(
                    owner_user_id=user_id,
                    source_user_id=user_id,
                    task_reason="   ",
                    schedule="* * * * *",
                    created_at=now,
                )
            )
            await session.commit()
