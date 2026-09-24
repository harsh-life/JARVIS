"""Operator control endpoints — 18 §5.4.

| Endpoint | Effect |
|---|---|
| `POST /api/v1/admin/control/stop` `{scope, target_id, reason}` | stop one task, or every task of a user or a device |
| `POST /api/v1/admin/control/global-stop` `{reason}` | stop every task and refuse new submissions until cleared |
| `POST /api/v1/admin/control/global-clear` | clear the global latch |

Every route depends on `get_superuser` (`Authorization: Superuser <token>`); an
ordinary Bearer token is refused before the handler runs (DASH-004,
SUPER-001). These are control routes, owned by the supervisor — not dashboard
routes, which stay read-only (DASH-002, 28 §1).

`reason` is an identifier (`^[a-z][a-z0-9_]{0,31}$`), not prose: it is recorded
in the audit trail, which carries no free-form text.

Stops only ever stop: there is no route here, or anywhere, that resumes a
stopped task (18 §5.2, §6). Clearing the latch lets new tasks be submitted;
it does not restart the ones it stopped.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from server.gateway.control_port import (
    ControlScope,
    ControlTargetNotFound,
    LatchReport,
    StopReport,
    SupervisorControlPort,
)
from server.gateway.deps import get_audit_logger, get_db_session
from server.gateway.errors import AppError
from server.gateway.superuser_auth import SuperuserPrincipal, get_superuser
from server.security.audit import AuditLogger
from shared.schemas.errors import ErrorCode

router = APIRouter(prefix="/admin/control", tags=["admin-control"])

_REASON = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")


class StopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: ControlScope
    target_id: uuid.UUID
    reason: str = _REASON


class GlobalStopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = _REASON


class GlobalClearRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskOutcomes(BaseModel):
    stopped: list[uuid.UUID]
    signalled: list[uuid.UUID]
    already_terminal: list[uuid.UUID]

    @classmethod
    def of(cls, report: StopReport) -> "TaskOutcomes":
        return cls(stopped=report.stopped, signalled=report.signalled,
                   already_terminal=report.already_terminal)


class StopResponse(BaseModel):
    scope: ControlScope
    target_id: uuid.UUID
    reason: str
    tasks: TaskOutcomes


class LatchResponse(BaseModel):
    latched: bool
    changed: bool
    tasks: TaskOutcomes

    @classmethod
    def of(cls, report: LatchReport) -> "LatchResponse":
        return cls(latched=report.latched, changed=report.changed, tasks=TaskOutcomes.of(report.tasks))


def _control(request: Request) -> SupervisorControlPort:
    control = getattr(request.app.state, "supervisor_control", None)
    if control is None:
        raise AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "operator controls are not configured on this server",
            details={"dependency": "supervisor"},
        )
    return control


@router.post("/stop", response_model=StopResponse)
async def stop(
    body: StopRequest,
    request: Request,
    principal: SuperuserPrincipal = Depends(get_superuser),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditLogger = Depends(get_audit_logger),
) -> StopResponse:
    try:
        report = await _control(request).stop(
            session, audit, principal=principal, scope=body.scope, target_id=body.target_id,
            reason=body.reason,
        )
    except ControlTargetNotFound:
        raise AppError(ErrorCode.NOT_FOUND, "no such task") from None
    return StopResponse(scope=body.scope, target_id=body.target_id, reason=body.reason,
                        tasks=TaskOutcomes.of(report))


@router.post("/global-stop", response_model=LatchResponse)
async def global_stop(
    body: GlobalStopRequest,
    request: Request,
    principal: SuperuserPrincipal = Depends(get_superuser),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditLogger = Depends(get_audit_logger),
) -> LatchResponse:
    return LatchResponse.of(
        await _control(request).global_stop(session, audit, principal=principal, reason=body.reason)
    )


@router.post("/global-clear", response_model=LatchResponse)
async def global_clear(
    request: Request,
    body: GlobalClearRequest | None = None,
    principal: SuperuserPrincipal = Depends(get_superuser),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditLogger = Depends(get_audit_logger),
) -> LatchResponse:
    return LatchResponse.of(await _control(request).global_clear(session, audit, principal=principal))
