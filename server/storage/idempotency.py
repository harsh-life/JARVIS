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
) -> StoredResult:
    """The core guarantee: a repeat call with the same key and the same
    request shape returns the original result without calling `execute`
    again. A repeat with the same key but a *different* request shape is a
    conflict, not a silent overwrite.
    """

    fp = fingerprint_request(method, path, body)

    existing = await session.get(IdempotencyKey, idempotency_key)
    if existing is not None:
        if existing.request_fingerprint != fp:
            raise IdempotencyConflict(
                f"idempotency key {idempotency_key!r} was already used for a "
                "different request"
            )
        return StoredResult(existing.status_code, existing.response_body)

    status_code, response_body = await execute()

    session.add(
        IdempotencyKey(
            idempotency_key=idempotency_key,
            request_fingerprint=fp,
            status_code=status_code,
            response_body=response_body,
            created_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    return StoredResult(status_code, response_body)
