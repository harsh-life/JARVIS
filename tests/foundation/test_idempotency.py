"""Idempotency-key abstraction tests (02_API_PROTOCOL.md §1.4).

These exercise server/storage/idempotency.py directly, not via an HTTP
endpoint — no state-changing endpoint exists yet in this branch (auth is
out of scope), and the instructions explicitly say not to pretend
otherwise: "Do not pretend idempotency has been implemented for operations
that do not yet exist."
"""

from __future__ import annotations

from server.storage import SQLAlchemyStorageBackend
from server.storage.idempotency import IdempotencyConflict, get_or_execute


async def test_same_key_same_body_returns_original_without_reexecuting(
    storage: SQLAlchemyStorageBackend,
) -> None:
    calls = {"count": 0}

    async def execute() -> tuple[int, dict]:
        calls["count"] += 1
        return 201, {"created": True, "call_number": calls["count"]}

    async with storage.session() as session:
        first = await get_or_execute(
            session,
            idempotency_key="key-1",
            method="POST",
            path="/api/v1/jobs",
            body={"task_reason": "pay rent"},
            execute=execute,
        )

    async with storage.session() as session:
        second = await get_or_execute(
            session,
            idempotency_key="key-1",
            method="POST",
            path="/api/v1/jobs",
            body={"task_reason": "pay rent"},
            execute=execute,
        )

    assert calls["count"] == 1  # execute() ran exactly once
    assert first.status_code == second.status_code == 201
    assert first.response_body == second.response_body == {"created": True, "call_number": 1}


async def test_different_key_executes_independently(storage: SQLAlchemyStorageBackend) -> None:
    calls = {"count": 0}

    async def execute() -> tuple[int, dict]:
        calls["count"] += 1
        return 201, {"call_number": calls["count"]}

    async with storage.session() as session:
        await get_or_execute(
            session, idempotency_key="key-a", method="POST", path="/x", body={}, execute=execute
        )
    async with storage.session() as session:
        await get_or_execute(
            session, idempotency_key="key-b", method="POST", path="/x", body={}, execute=execute
        )

    assert calls["count"] == 2


async def test_same_key_different_body_is_a_conflict(storage: SQLAlchemyStorageBackend) -> None:
    async def execute() -> tuple[int, dict]:
        return 201, {"ok": True}

    async with storage.session() as session:
        await get_or_execute(
            session,
            idempotency_key="key-conflict",
            method="POST",
            path="/api/v1/jobs",
            body={"task_reason": "pay rent"},
            execute=execute,
        )

    import pytest

    with pytest.raises(IdempotencyConflict):
        async with storage.session() as session:
            await get_or_execute(
                session,
                idempotency_key="key-conflict",
                method="POST",
                path="/api/v1/jobs",
                body={"task_reason": "DIFFERENT REASON"},
                execute=execute,
            )
