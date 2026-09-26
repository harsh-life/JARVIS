"""Session endpoints — 02 §3 (token, logout) and 02 §4 (active graph).

The active-graph switch is the clearest illustration of PHONE-003 in the whole
API: the client sends a `graph_id` it wants, and that value is treated as *a
claim to check*. It goes through the authorization engine before it is recorded,
and a graph the caller is not a member of yields `404` — never the graph.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.sessions import ResolvedSession
from server.gateway.deps import (
    get_audit_logger,
    get_db_session,
    get_resolved_session,
    get_security_core,
)
from server.gateway.errors import AppError
from server.gateway.security import SecurityCore
from server.graph.authorization import AccessRequest
from server.security.audit import AuditLogger
from shared.schemas.authorization import DenialSurface, Operation, ResourceType
from shared.schemas.device_channel import DeviceCloseCode
from shared.schemas.errors import ErrorCode

router = APIRouter(tags=["sessions"])


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_credential: str


class TokenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: str
    expires_at: datetime


class ActiveGraphRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    graph_id: uuid.UUID


class ActiveGraphResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID
    active_graph_id: uuid.UUID


@router.post("/sessions/token", response_model=TokenResponse)
async def issue_token(
    body: TokenRequest,
    session: AsyncSession = Depends(get_db_session),
    core: SecurityCore = Depends(get_security_core),
    audit: AuditLogger = Depends(get_audit_logger),
) -> TokenResponse:
    """SESSION-001 (02 §3, 03 §5.1) — refresh without a full re-login.

    Two steps in a fixed order, neither skippable: verify the device-credential
    proof, then issue a token for the device the proof identified. The user id on
    the resulting session comes from the `Device` row, never from this request
    (AUTH-T10).

    Every rejection in 03 §5.4 — revoked device, rotated credential, invalid
    credential, inactive user — arrives as `401` with no detail, and the client's
    documented recovery is the full Google login.
    """

    device = await core.devices.verify_proof(session, body.device_credential, audit=audit)
    issued = await core.sessions.issue(session, device=device, audit=audit)
    return TokenResponse(access_token=issued.access_token, expires_at=issued.expires_at)


@router.post("/sessions/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    core: SecurityCore = Depends(get_security_core),
    resolved: ResolvedSession = Depends(get_resolved_session),
    audit: AuditLogger = Depends(get_audit_logger),
) -> Response:
    """03 §6 — end this session. Does not revoke the device credential."""

    await core.sessions.logout(session, resolved=resolved, audit=audit)
    # The device's socket was authenticated by a session that just ended —
    # closed only once that end is committed.
    hub = getattr(request.app.state, "device_hub", None)
    if hub is not None:
        await session.commit()
        await hub.disconnect(resolved.principal.device_id, DeviceCloseCode.AUTH_EXPIRED, "logged out")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sessions/active-graph", response_model=ActiveGraphResponse)
async def set_active_graph(
    body: ActiveGraphRequest,
    session: AsyncSession = Depends(get_db_session),
    core: SecurityCore = Depends(get_security_core),
    resolved: ResolvedSession = Depends(get_resolved_session),
    audit: AuditLogger = Depends(get_audit_logger),
) -> ActiveGraphResponse:
    """GRAPH-006 (02 §4) — switch the session's active graph.

    Membership is checked through the authorization engine, not here, so there is
    one implementation of D1 (§9 of the security-core scope).

    A non-member gets `404`. 02 §4's error column lists both `403` and `404` for
    this row, and the `[LOCKED]` sentence immediately below that table resolves
    the ambiguity — "a client asserting `graph_id` it isn't a member of gets
    `404` (anti-enumeration), never the graph" — which is also what 04 §7
    requires. The engine's `surface` field carries that decision here rather than
    this handler inferring it.
    """

    outcome = await core.engine.authorize(
        session,
        AccessRequest(
            principal=resolved.principal,
            operation=Operation.READ,
            resource_type=ResourceType.GRAPH,
            resource_ref=str(body.graph_id),
            graph_id=body.graph_id,
        ),
        audit=audit,
    )

    if not outcome.allowed:
        raise AppError(
            ErrorCode.NOT_FOUND
            if outcome.surface is DenialSurface.NOT_FOUND
            else ErrorCode.UNAUTHORIZED,
            "not found" if outcome.surface is DenialSurface.NOT_FOUND else "not permitted",
        )

    updated = await core.sessions.set_active_graph(
        session, resolved=resolved, graph_id=body.graph_id, audit=audit
    )
    if updated is None:
        raise AppError(ErrorCode.UNAUTHENTICATED, "authentication failed")

    return ActiveGraphResponse(
        session_id=updated.session_id,
        active_graph_id=body.graph_id,
    )
