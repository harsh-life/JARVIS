"""Dependency-injection scaffolding.

Foundation provided exactly one dependency (`get_db_session`); security-core adds
the auth-derived ones 03 defines.

## One transaction, with commit semantics chosen to satisfy 02 §1.2

02 §1.2 requires that a failed request produce "the appropriate error, an
`AuditEvent` (`01` §11.1), and **no side effect**" — audit entries must survive a
refusal, while the refused mutation must not.

Audit and business work therefore share **one** session. A second session is not
an option on the pilot's SQLite backend (STORE-004): SQLite permits a single
writer, so an audit connection flushing while the business connection holds an
uncommitted write lock deadlocks rather than interleaving.

With one session, the guarantee comes from *which* exceptions commit:

* **clean return** → commit;
* **a security refusal** (`AppError`, `AuthError`, `AbsoluteFloorViolation`,
  `GraphOperationRefused`) → commit. Every one of these is raised *before* any
  mutation — the engine denies before a router writes, and each service validates
  before it writes — so what commits is the audit trail and nothing else, which is
  exactly 02 §1.2;
* **anything else** → roll back. An unexpected error may have left a partial
  mutation, and discarding it is worth losing that request's audit rows; the
  `500` and the server log still record that it happened.

## Identity comes only from the token

`get_resolved_session` reads the `Authorization` header and nothing else. There is
no dependency here that accepts a `user_id`, `device_id`, `session_id`, or
`graph_id` from a path, query, or body — so PHONE-003 holds because the wiring
offers no alternative, not because each handler remembers (AUTH-T7).
"""

from __future__ import annotations

import uuid
from typing import AsyncIterator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.errors import AuthError, InvalidAccessToken
from server.auth.sessions import ResolvedSession
from server.capabilities.floor import AbsoluteFloorViolation
from server.gateway.errors import AppError
from server.gateway.security import SecurityCore
from server.graph.service import GraphOperationRefused
from server.security.audit import AuditLogger
from server.storage import StorageBackend
from shared.schemas.authorization import Principal

BEARER_PREFIX = "Bearer "


def get_storage_backend(request: Request) -> StorageBackend:
    return request.app.state.storage


def get_security_core(request: Request) -> SecurityCore:
    return request.app.state.security


def get_request_id(request: Request) -> uuid.UUID:
    return request.state.request_id


# Exceptions that are raised before any mutation, so committing on them persists
# the audit trail and nothing else (02 §1.2). Listed explicitly rather than
# caught broadly: a new failure type must be considered against that property
# before it joins this tuple.
SECURITY_REFUSALS: tuple[type[BaseException], ...] = (
    AppError,
    AuthError,
    AbsoluteFloorViolation,
    GraphOperationRefused,
)


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """The request's single transaction. See the module docstring for why.

    `request.state.db_session` is set so `get_audit_logger` can reuse the very
    same session — FastAPI would otherwise resolve the dependency twice within
    one request only if the cache were bypassed, and relying on that cache for a
    correctness property is too subtle to leave implicit.
    """

    backend: StorageBackend = request.app.state.storage
    async with backend.session() as session:
        request.state.db_session = session
        try:
            yield session
        except SECURITY_REFUSALS:
            await session.commit()
            raise
        except BaseException:
            await session.rollback()
            raise
        else:
            await session.commit()


async def get_audit_logger(
    request: Request, session: AsyncSession = Depends(get_db_session)
) -> AuditLogger:
    """The per-request audit writer, bound to the request's transaction."""

    return AuditLogger(session, request_id=request.state.request_id)


def bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization") or ""
    if not header.startswith(BEARER_PREFIX):
        raise InvalidAccessToken("missing_bearer")
    return header[len(BEARER_PREFIX) :].strip()


async def get_resolved_session(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    core: SecurityCore = Depends(get_security_core),
) -> ResolvedSession:
    """02 §1.1: validate the access token and resolve the principal from it.

    Every failure raises an `AuthError`, which
    `server/gateway/security_errors.py` maps to `401`. There is no path that
    returns an anonymous or partial principal.
    """

    return await core.sessions.resolve_principal(session, bearer_token(request))


async def get_principal(
    resolved: ResolvedSession = Depends(get_resolved_session),
) -> Principal:
    """03 §8's handoff contract, for handlers that need identity but not
    token freshness."""

    return resolved.principal
