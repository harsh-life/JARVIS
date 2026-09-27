"""The Judge's operator controls — 19 §9, 28 §1.

| Endpoint | Effect |
|---|---|
| `POST /api/v1/admin/control/evaluation/switches` `{judge_enabled?, stop_requests_enabled?, reason}` | turn the Judge / its stop requests off, or back on up to what the config allows |
| `POST /api/v1/admin/control/evaluation/candidates/{id}/approve` `{reason}` | approve one pending improvement candidate → a new config version |
| `POST /api/v1/admin/control/evaluation/candidates/{id}/reject` `{reason}` | reject it |
| `POST /api/v1/admin/control/evaluation/config-versions/{version_id}/rollback` `{reason}` | undo a target's current version |

Every route depends on `get_superuser`; an ordinary Bearer token is refused
before the handler runs (DASH-004, SUPER-001). These are control routes, owned
by the subsystem that enforces them — never by the read-only dashboard
(DASH-002, 28 §1). `reason` is an identifier, recorded in the audit trail.

Nothing here resumes a task, grants a capability, lowers a tier, satisfies a
confirmation or changes security policy: an approved candidate can only set a
value the closed registry allows (`server/evaluation/candidates.py`), and it
is re-validated at approval.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from server.gateway.deps import get_audit_logger, get_db_session
from server.gateway.errors import AppError
from server.gateway.evaluation_control_port import (
    EvaluationControlPort,
    EvaluationControlRefusalKind,
    EvaluationControlRefused,
)
from server.gateway.superuser_auth import SuperuserPrincipal, get_superuser
from server.security.audit import AuditLogger
from shared.schemas.errors import ErrorCode
from shared.schemas.evaluation import IDENTIFIER_PATTERN

router = APIRouter(prefix="/admin/control/evaluation", tags=["admin-control"])

_REASON = Field(pattern=IDENTIFIER_PATTERN)

_ERRORS = {
    EvaluationControlRefusalKind.CONFLICT: ErrorCode.CONFLICT,
    EvaluationControlRefusalKind.NOT_FOUND: ErrorCode.NOT_FOUND,
    EvaluationControlRefusalKind.VALIDATION: ErrorCode.VALIDATION_FAILED,
}


class SwitchesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    judge_enabled: bool | None = None
    stop_requests_enabled: bool | None = None
    reason: str = _REASON


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = _REASON


class SwitchesResponse(BaseModel):
    configured_enabled: bool
    configured_may_request_stop: bool
    judge_enabled: bool
    stop_requests_enabled: bool


class DecisionResponse(BaseModel):
    candidate_id: uuid.UUID
    status: str
    config_version_id: int | None
    target_key: str


class VersionResponse(BaseModel):
    version_id: int
    target_key: str
    action: str
    rolled_back_version_id: int | None


def _control(request: Request) -> EvaluationControlPort:
    control = getattr(request.app.state, "evaluation_control", None)
    if control is None:
        raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "evaluation controls are not configured on this server",
                       details={"dependency": "evaluation"})
    return control


def _refused(exc: EvaluationControlRefused) -> AppError:
    return AppError(_ERRORS[exc.kind], str(exc), details={"refusal": exc.code})


@router.post("/switches", response_model=SwitchesResponse)
async def set_switches(
    body: SwitchesRequest,
    request: Request,
    principal: SuperuserPrincipal = Depends(get_superuser),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditLogger = Depends(get_audit_logger),
) -> SwitchesResponse:
    try:
        view = await _control(request).set_switches(
            session, audit, principal=principal, judge_enabled=body.judge_enabled,
            stop_requests_enabled=body.stop_requests_enabled, reason=body.reason,
        )
    except EvaluationControlRefused as exc:
        raise _refused(exc) from None
    return SwitchesResponse(**vars(view))


@router.post("/candidates/{candidate_id}/approve", response_model=DecisionResponse)
async def approve_candidate(
    candidate_id: uuid.UUID,
    body: DecisionRequest,
    request: Request,
    principal: SuperuserPrincipal = Depends(get_superuser),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditLogger = Depends(get_audit_logger),
) -> DecisionResponse:
    try:
        view = await _control(request).approve_candidate(
            session, audit, principal=principal, candidate_id=candidate_id, reason=body.reason,
        )
    except EvaluationControlRefused as exc:
        raise _refused(exc) from None
    return DecisionResponse(**vars(view))


@router.post("/candidates/{candidate_id}/reject", response_model=DecisionResponse)
async def reject_candidate(
    candidate_id: uuid.UUID,
    body: DecisionRequest,
    request: Request,
    principal: SuperuserPrincipal = Depends(get_superuser),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditLogger = Depends(get_audit_logger),
) -> DecisionResponse:
    try:
        view = await _control(request).reject_candidate(
            session, audit, principal=principal, candidate_id=candidate_id, reason=body.reason,
        )
    except EvaluationControlRefused as exc:
        raise _refused(exc) from None
    return DecisionResponse(**vars(view))


@router.post("/config-versions/{version_id}/rollback", response_model=VersionResponse)
async def rollback_version(
    version_id: int,
    body: DecisionRequest,
    request: Request,
    principal: SuperuserPrincipal = Depends(get_superuser),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditLogger = Depends(get_audit_logger),
) -> VersionResponse:
    try:
        view = await _control(request).rollback_version(
            session, audit, principal=principal, version_id=version_id, reason=body.reason,
        )
    except EvaluationControlRefused as exc:
        raise _refused(exc) from None
    return VersionResponse(**vars(view))
