"""Capability endpoints — 02 §6.

`[LOCKED]` (02 §6) "A `POST /capabilities` for an absolute-floor capability
(PERM-006) returns `prohibited` and creates no grant (`01` §7.1 validation)."
That is enforced in `server/capabilities/grants.py`, which raises before it
writes anything; this router only maps the exception onto the status code.

Note what is absent: there is no endpoint here by which the agent could grant
itself anything. `POST /capabilities` requires an authenticated *user* principal
and records them as `granted_by` (PERM-002's consent act), and `server.agent`
cannot import the grant service at all (pyproject's import-linter contract).
Capability grants are user consent, not runtime state.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from server.capabilities.grants import CapabilityGrantRefused
from server.gateway.deps import (
    get_audit_logger,
    get_db_session,
    get_principal,
    get_security_core,
)
from server.gateway.errors import AppError
from server.gateway.security import SecurityCore
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from shared.schemas.authorization import Principal
from shared.schemas.capability import CapabilityGrant as CapabilityGrantContract
from shared.schemas.enums import AuditActor, AuditResult, CapabilityScopeType
from shared.schemas.errors import ErrorCode

router = APIRouter(tags=["capabilities"])


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str
    scope_type: CapabilityScopeType
    # 01 §7.1: `principal_id` is "the user/device/session/graph/task the grant
    # applies to (per `scope_type`)" — so this client-facing `scope_id` *is* that
    # id. 02 §6 names the field `scope_id`; the storage column is `principal_id`.
    scope_id: uuid.UUID
    resource_scope: dict[str, str] | None = None
    expires_at: datetime | None = None


class GrantListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CapabilityGrantContract]


@router.get("/capabilities", response_model=GrantListResponse)
async def list_capabilities(
    session: AsyncSession = Depends(get_db_session),
    core: SecurityCore = Depends(get_security_core),
    principal: Principal = Depends(get_principal),
) -> GrantListResponse:
    """02 §6 — the caller's own active grants.

    Scoped to the ids that *are* this caller: their user, device, and session. A
    grant keyed on another principal is not in the query's range at all, so this
    listing cannot disclose what another user has authorized.
    """

    grants = await core.capability_grants.list_active(
        session,
        principal_ids={
            principal.user_id,
            principal.device_id,
            principal.session_id,
        },
    )
    return GrantListResponse(
        items=[CapabilityGrantContract.model_validate(g) for g in grants]
    )


@router.post(
    "/capabilities",
    response_model=CapabilityGrantContract,
    status_code=status.HTTP_201_CREATED,
)
async def grant_capability(
    body: GrantRequest,
    session: AsyncSession = Depends(get_db_session),
    core: SecurityCore = Depends(get_security_core),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> CapabilityGrantContract:
    """PERM-002 — user consent creates a capability grant.

    A caller may grant only within their own reach: a user/device/session scope
    must name *their* id. Without that check, any authenticated user could mint a
    grant for another user's device — the request names the scope id, and 02 §1.1
    is explicit that such a field is a claim to validate, never an authorization.

    A graph-scoped grant additionally requires the caller to be that graph's
    owner (04 §2 D2: administering graph-level configuration is owner-only).
    """

    own_ids = {
        CapabilityScopeType.USER: principal.user_id,
        CapabilityScopeType.DEVICE: principal.device_id,
        CapabilityScopeType.SESSION: principal.session_id,
    }
    if body.scope_type in own_ids:
        # The id must be the caller's own id *of that kind*: a device-scoped
        # grant keyed on a user id matches nothing (`_scope_matches`), so
        # accepting it would record consent that authorizes nothing while
        # looking like authority in every listing.
        if body.scope_id != own_ids[body.scope_type]:
            raise AppError(ErrorCode.UNAUTHORIZED, "not permitted")
    elif body.scope_type is CapabilityScopeType.GRAPH:
        role = await core.graph_repository.active_role(
            session, graph_id=body.scope_id, user_id=principal.user_id
        )
        if role is None:
            raise AppError(ErrorCode.NOT_FOUND, "not found")
        if role.value != "owner":
            raise AppError(ErrorCode.UNAUTHORIZED, "not permitted")
    # CapabilityScopeType.TASK grants are bound to an agent task, which only the
    # runtime branch can create; no client-facing path exists for one yet.
    else:
        raise AppError(ErrorCode.VALIDATION_FAILED, "unsupported scope_type")

    try:
        grant = await core.capability_grants.grant(
            session,
            principal_id=body.scope_id,
            scope_type=body.scope_type,
            capability=body.capability,
            granted_by=principal.user_id,
            resource_scope=body.resource_scope,
            expires_at=body.expires_at,
        )
    except CapabilityGrantRefused as exc:
        await audit.record(
            actor=AuditActor.USER,
            action=AuditAction.CAPABILITY_GRANT_REFUSED,
            resource=f"capability:{body.capability}",
            result=AuditResult.BLOCKED,
            user_id=principal.user_id,
        )
        raise AppError(ErrorCode.VALIDATION_FAILED, str(exc)) from exc

    await audit.record(
        actor=AuditActor.USER,
        action=AuditAction.CAPABILITY_GRANTED,
        resource=f"capability_grant:{grant.grant_id}",
        result=AuditResult.SUCCESS,
        user_id=principal.user_id,
    )
    return CapabilityGrantContract.model_validate(grant)


@router.delete("/capabilities/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_capability(
    grant_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    core: SecurityCore = Depends(get_security_core),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> Response:
    """02 §6 — revoke a grant.

    PRD §13: "Revocation is immediate: toggling off revokes the grant, and the
    next device operation for that app fails authorization." A grant that is not
    the caller's is reported as absent (04 §7).
    """

    grant = await core.capability_grants.revoke(
        session, grant_id=grant_id, revoked_by=principal.user_id
    )
    if grant is None:
        raise AppError(ErrorCode.NOT_FOUND, "not found")

    await audit.record(
        actor=AuditActor.USER,
        action=AuditAction.CAPABILITY_REVOKED,
        resource=f"capability_grant:{grant_id}",
        result=AuditResult.SUCCESS,
        user_id=principal.user_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
