"""Idempotency-key abstraction (02_API_PROTOCOL.md §1.4).

"[IMPL] storage of idempotency keys, but the guarantee is not optional for
those endpoints (prevents duplicate charges/actions on client retry after
FAIL-001)." No endpoint in this branch is state-changing yet (auth doesn't
exist), so nothing here is wired to a route — this module is the reusable
piece a later endpoint calls into, exercised directly by
tests/foundation/test_idempotency.py.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from server.storage.models import IdempotencyKey


class IdempotencyConflict(Exception):
    """The same Idempotency-Key was reused with a different request body.

    02_API_PROTOCOL.md doesn't lock the exact behavior for this case; the
    `conflict` error code (§1.7, HTTP 409) is the natural fit for whichever
    later endpoint wires this in — this module only raises the condition.
    """


class IdempotencyInProgress(IdempotencyConflict):
    """The same key arrived again while its first request is still running.

    H-1: once requests run concurrently (they used to queue on SQLite's one
    writer), a retry could otherwise find no stored result yet and run the
    action a second time. It is refused instead — a `409 conflict` like any
    other idempotency conflict (02 §1.7) — and once the original has answered,
    the same key replays it as usual.
    """


# Keys whose first request is running now. The pilot is one server process
# (docs/RUNNING_RUNTIME.md §4a); across processes the table's primary key
# still refuses a second stored result.
_IN_FLIGHT: set[str] = set()


@dataclass(frozen=True)
class StoredResult:
    status_code: int
    response_body: dict


def fingerprint_request(method: str, path: str, body: dict | None) -> str:
    """A stable hash of the request shape, used to detect a key reused with
    a *different* payload (as opposed to a genuine retry)."""

    payload = json.dumps({"method": method, "path": path, "body": body or {}}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def get_or_execute(
    session: AsyncSession,
    *,
    idempotency_key: str,
    method: str,
    path: str,
    body: dict | None,
    execute: Callable[[], Awaitable[tuple[int, dict]]],
    redact_for_storage: Callable[[dict], dict] | None = None,
) -> StoredResult:
    """The core guarantee: a repeat call with the same key and the same
    request shape returns the original result without calling `execute`
    again. A repeat with the same key but a *different* request shape is a
    conflict, not a silent overwrite.

    `redact_for_storage` shapes the copy that is *persisted* for replay; the
    first caller still receives the full response. It exists for credentials a
    response legitimately carries once — a confirmation token — which must not
    outlive that response in plaintext in this table.
    """

    fp = fingerprint_request(method, path, body)

    # Claimed before the first `await`, and let go only after the result is
    # committed: a same-key request either sees it running or reads its result.
    if idempotency_key in _IN_FLIGHT:
        raise IdempotencyInProgress(f"a request with idempotency key {idempotency_key!r} is still running")
    _IN_FLIGHT.add(idempotency_key)
    try:
        existing = await session.get(IdempotencyKey, idempotency_key)
        if existing is not None:
            if existing.request_fingerprint != fp:
                raise IdempotencyConflict(
                    f"idempotency key {idempotency_key!r} was already used for a "
                    "different request"
                )
            return StoredResult(existing.status_code, existing.response_body)

        status_code, response_body = await execute()
        stored_body = redact_for_storage(response_body) if redact_for_storage else response_body

        session.add(
            IdempotencyKey(
                idempotency_key=idempotency_key,
                request_fingerprint=fp,
                status_code=status_code,
                response_body=stored_body,
                created_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
        return StoredResult(status_code, response_body)
    finally:
        _IN_FLIGHT.discard(idempotency_key)
