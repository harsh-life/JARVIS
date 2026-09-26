"""Memory endpoints — 02 §7, visibility enforced by `11` through `04`.

`[LOCKED]` (02 §7) `GET /memory` returns only facts where `visibility==graph`
(for a graph the caller is an active member of) OR `owner_user_id==caller` —
being in the graph is not enough to see another user's private facts. A
`fact_type` outside the enum is `422` (EMO-002). Correction, sharing and deletion
are owner-only (D3); sharing and deletion are `consequential` in the tier table,
so they return `403 confirmation_required` with a single-use token bound to the
exact action, and run only when that token comes back in `X-Confirmation-Token`.

Without a configured memory provider every endpoint answers `503
dependency_unavailable` with `dependency: mem0` (02 §13, FAIL-008).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from server.gateway.deps import get_audit_logger, get_db_session, get_principal
from server.gateway.errors import AppError
from server.gateway.memory_port import MemoryPort
from server.security.audit import AuditLogger
from server.storage.idempotency import IdempotencyConflict, get_or_execute
from shared.schemas.authorization import Principal
from shared.schemas.errors import ErrorCode
from shared.schemas.memory import (
    Mem0Fact,
    MemoryCreateRequest,
    MemoryListResponse,
    MemoryPatchRequest,
)

router = APIRouter(tags=["memory"])

MAX_PAGE_SIZE = 100
MAX_QUERY_CHARS = 1000
MAX_IDEMPOTENCY_KEY_LENGTH = 200
CONFIRMATION_HEADER = "X-Confirmation-Token"


def _memory(request: Request) -> MemoryPort:
    port = getattr(request.app.state, "memory", None)
    if port is None:
        raise AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "long-term memory is not available on this server",
            details={"dependency": "mem0"},
        )
    return port


@router.get("/memory", response_model=MemoryListResponse)
async def list_memory(
    request: Request,
    query: str | None = None,
    limit: int = 20,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> MemoryListResponse:
    if query is not None and len(query) > MAX_QUERY_CHARS:
        raise AppError(ErrorCode.VALIDATION_FAILED, "query is too long")
    capped = max(1, min(limit, MAX_PAGE_SIZE))
    return await _memory(request).list(
        session, principal=principal, query=query, limit=capped, audit=audit
    )


@router.post("/memory", response_model=Mem0Fact, status_code=status.HTTP_201_CREATED)
async def add_memory(
    request: Request,
    body: MemoryCreateRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Mem0Fact:
    port = _memory(request)

    async def _execute() -> tuple[int, dict]:
        fact = await port.add(session, principal=principal, body=body, audit=audit)
        return status.HTTP_201_CREATED, fact.model_dump(mode="json")

    if idempotency_key:
        if len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
            raise AppError(ErrorCode.VALIDATION_FAILED, "Idempotency-Key is too long")
        try:
            stored = await get_or_execute(
                session,
                # Namespaced per user: a replayed key never returns another
                # user's stored response.
                idempotency_key=f"{principal.user_id}:{idempotency_key}",
                method="POST",
                path="/api/v1/memory",
                body=body.model_dump(mode="json"),
                execute=_execute,
                # Only the id is kept for replay. Storing the whole response
                # would leave a copy of the fact's text in this table after the
                # owner deletes the fact.
                redact_for_storage=lambda response: {"fact_id": response["fact_id"]},
            )
        except IdempotencyConflict as exc:
            raise AppError(ErrorCode.CONFLICT, str(exc)) from exc
        if "content" in stored.response_body:
            return Mem0Fact.model_validate(stored.response_body)
        # A replay: re-read through the engine, so a since-deleted fact is 404.
        return await port.recall(
            session, principal=principal, fact_id=uuid.UUID(stored.response_body["fact_id"]), audit=audit
        )

    _status, payload = await _execute()
    return Mem0Fact.model_validate(payload)


@router.patch("/memory/{fact_id}", response_model=Mem0Fact)
async def patch_memory(
    request: Request,
    fact_id: uuid.UUID,
    body: MemoryPatchRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
    confirmation_token: str | None = Header(default=None, alias=CONFIRMATION_HEADER, max_length=512),
) -> Mem0Fact:
    return await _memory(request).patch(
        session, principal=principal, fact_id=fact_id, body=body,
        confirmation_token=confirmation_token, audit=audit,
    )


@router.delete("/memory/{fact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    request: Request,
    fact_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
    confirmation_token: str | None = Header(default=None, alias=CONFIRMATION_HEADER, max_length=512),
) -> Response:
    await _memory(request).delete(
        session, principal=principal, fact_id=fact_id,
        confirmation_token=confirmation_token, audit=audit,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/memory/status")
async def memory_status(
    request: Request, principal: Principal = Depends(get_principal)
) -> dict:
    """Whether persistent memory is enabled and answering. No content, no counts."""

    port = getattr(request.app.state, "memory", None)
    if port is None:
        return {"enabled": False, "available": False}
    return await port.status()
