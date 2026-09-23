"""Graph and membership endpoints — 02 §4, authorization enforced by `04`.

`[LOCKED]` (02 §4) "Every graph endpoint re-checks membership server-side
(PHONE-003) — a client asserting `graph_id` it isn't a member of gets `404`
(anti-enumeration), never the graph." Each handler below goes through
`GraphService`, which re-derives the caller's role from the membership table on
every call; no handler trusts a role, an ownership claim, or a graph id from the
request.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from server.gateway.deps import (
    get_audit_logger,
    get_db_session,
    get_principal,
    get_security_core,
)
from server.gateway.errors import AppError
from server.gateway.security import SecurityCore
from server.security.audit import AuditLogger
from server.storage.idempotency import IdempotencyConflict, get_or_execute
from shared.schemas.authorization import Principal
from shared.schemas.enums import GraphType, MembershipRole
from shared.schemas.errors import ErrorCode
from shared.schemas.graph import Graph as GraphContract
from shared.schemas.graph import GraphMembership as MembershipContract

router = APIRouter(tags=["graphs"])

# 02 §1.8: "`limit` is capped server-side (RATE-001 spirit — no unbounded fetch)."
MAX_PAGE_SIZE = 100

MAX_IDEMPOTENCY_KEY_LENGTH = 200


def _scoped_idempotency_key(principal: Principal, raw: str) -> str:
    """The stored key is namespaced by the authenticated user.

    `get_or_execute` returns a stored response *before* `execute` runs, so a
    shared key namespace would hand user B user A's stored response — without
    any authorization check — just by replaying A's key with the same body.
    """

    if len(raw) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise AppError(ErrorCode.VALIDATION_FAILED, "Idempotency-Key is too long")
    return f"{principal.user_id}:{raw}"


class CreateGraphRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    type: GraphType


class GraphListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[GraphContract]
    next_cursor: str | None = None


class AccessRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str | None = Field(default=None, max_length=1000)


class AccessRequestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: uuid.UUID
    status: str


class ApproveMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID
    # 04 §6: a graph has exactly one owner, so approval never grants `owner`;
    # ownership moves only through transfer. Constraining the type here means the
    # request cannot even express it.
    role: MembershipRole = MembershipRole.MEMBER


@router.post("/graphs", response_model=GraphContract, status_code=status.HTTP_201_CREATED)
async def create_graph(
    body: CreateGraphRequest,
    session: AsyncSession = Depends(get_db_session),
    core: SecurityCore = Depends(get_security_core),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> GraphContract:
    """GRAPH-007 (02 §4) — the creator becomes owner.

    Idempotent per 02 §1.4: a retried create with the same key returns the
    original graph rather than a second one. The stored response body carries only
    the `Graph` entity, which holds no credential.
    """

    async def _execute() -> tuple[int, dict]:
        graph = await core.graphs.create_graph(
            session,
            name=body.name,
            graph_type=body.type,
            creator_user_id=principal.user_id,
            audit=audit,
        )
        return status.HTTP_201_CREATED, GraphContract.model_validate(graph).model_dump(
            mode="json"
        )

    if idempotency_key:
        try:
            stored = await get_or_execute(
                session,
                idempotency_key=_scoped_idempotency_key(principal, idempotency_key),
                method="POST",
                path="/api/v1/graphs",
                body=body.model_dump(mode="json"),
                execute=_execute,
            )
        except IdempotencyConflict as exc:
            raise AppError(ErrorCode.CONFLICT, str(exc)) from exc
        return GraphContract.model_validate(stored.response_body)

    _status, payload = await _execute()
    return GraphContract.model_validate(payload)


@router.get("/graphs", response_model=GraphListResponse)
async def list_graphs(
    limit: int = MAX_PAGE_SIZE,
    session: AsyncSession = Depends(get_db_session),
    core: SecurityCore = Depends(get_security_core),
    principal: Principal = Depends(get_principal),
) -> GraphListResponse:
    """ENT-002 (02 §4) — the graphs the caller is a member of.

    The scoping is in the query's membership join (`GraphRepository`), not a
    post-filter: a listing that fetched everything and then filtered is one
    refactor away from returning the unfiltered set.
    """

    capped = max(1, min(limit, MAX_PAGE_SIZE))
    graphs = await core.graph_repository.graphs_for_user(
        session, user_id=principal.user_id, limit=capped
    )
    return GraphListResponse(
        items=[GraphContract.model_validate(g) for g in graphs], next_cursor=None
    )


@router.post(
    "/graphs/{graph_id}/access-requests",
    response_model=AccessRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def request_access(
    graph_id: uuid.UUID,
    body: AccessRequestBody,
    session: AsyncSession = Depends(get_db_session),
    core: SecurityCore = Depends(get_security_core),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> AccessRequestResponse:
    """GRAPH-008 (02 §4, 04 §4.2) — ask; this grants nothing.

    `[LOCKED]` (04 §4.2) membership is created only by an owner's explicit
    approval — never self-service, never by the requester asserting it.
    """

    row = await core.graphs.request_access(
        session,
        graph_id=graph_id,
        requester_user_id=principal.user_id,
        message=body.message,
        audit=audit,
    )
    return AccessRequestResponse(request_id=row.request_id, status=row.status)


@router.post(
    "/graphs/{graph_id}/members",
    response_model=MembershipContract,
    status_code=status.HTTP_201_CREATED,
)
async def approve_member(
    graph_id: uuid.UUID,
    body: ApproveMemberRequest,
    session: AsyncSession = Depends(get_db_session),
    core: SecurityCore = Depends(get_security_core),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> MembershipContract:
    """GRAPH-008 (02 §4) — owner-only approval (D2, AZ-T6).

    The approver's owner role is read from the membership table inside
    `GraphService`; a `member` attempting this gets `403`, and a non-member gets
    `404` so the graph's existence is not disclosed.
    """

    async def _execute() -> tuple[int, dict]:
        membership = await core.graphs.approve_member(
            session,
            graph_id=graph_id,
            approver_user_id=principal.user_id,
            user_id=body.user_id,
            role=body.role,
            audit=audit,
        )
        return status.HTTP_201_CREATED, MembershipContract.model_validate(
            membership
        ).model_dump(mode="json")

    if idempotency_key:
        try:
            stored = await get_or_execute(
                session,
                idempotency_key=_scoped_idempotency_key(principal, idempotency_key),
                method="POST",
                path=f"/api/v1/graphs/{graph_id}/members",
                body=body.model_dump(mode="json"),
                execute=_execute,
            )
        except IdempotencyConflict as exc:
            raise AppError(ErrorCode.CONFLICT, str(exc)) from exc
        return MembershipContract.model_validate(stored.response_body)

    _status, payload = await _execute()
    return MembershipContract.model_validate(payload)


@router.delete(
    "/graphs/{graph_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def revoke_member(
    graph_id: uuid.UUID,
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    core: SecurityCore = Depends(get_security_core),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> Response:
    """04 §4.3 — an owner may revoke any member; a member may leave.

    Effect is immediate: the revoked user's next authorization check fails D1
    (AZ-T8), because `active_role` filters on `revoked_at` and is re-read live.
    """

    await core.graphs.revoke_membership(
        session,
        graph_id=graph_id,
        actor_user_id=principal.user_id,
        target_user_id=user_id,
        audit=audit,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
