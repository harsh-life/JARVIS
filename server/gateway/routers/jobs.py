"""Scheduler endpoints — 02 §9, docs/22 §1.

`[LOCKED]` (02 §9) an empty `task_reason` is `422` (SCHED-001, no unprompted
proactivity); creation counts against the per-user scheduler limit, `429` on
breach (RATE-001, FAIL-010). A reminder is the caller's own and `private`; a
`graph_id` in the body is a claim the engine checks (D1), never a grant.

Cancelling is `DELETE` on the job, which the tier table rates `consequential`
like every other delete — so it returns `403 confirmation_required` with a
single-use token bound to that exact cancel, and runs when the token comes
back in `X-Confirmation-Token` (the same flow as memory deletion).

Without a configured scheduler every endpoint answers `503
dependency_unavailable` with `dependency: scheduler` (02 §13).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from server.gateway.deps import get_audit_logger, get_db_session, get_principal
from server.gateway.errors import AppError
from server.gateway.scheduler_port import SchedulerPort
from server.security.audit import AuditLogger
from server.storage.idempotency import IdempotencyConflict, get_or_execute
from shared.schemas.authorization import Principal
from shared.schemas.errors import ErrorCode
from shared.schemas.scheduler import JobCreateRequest, JobListResponse, ScheduledJobView

router = APIRouter(tags=["scheduler"])

MAX_PAGE_SIZE = 100
MAX_CURSOR_LENGTH = 200
MAX_IDEMPOTENCY_KEY_LENGTH = 200
CONFIRMATION_HEADER = "X-Confirmation-Token"


def _scheduler(request: Request) -> SchedulerPort:
    port = getattr(request.app.state, "scheduler", None)
    if port is None:
        raise AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "couldn't set or manage reminders: the scheduler is not available",
            details={"dependency": "scheduler"},
        )
    return port


@router.post("/jobs", response_model=ScheduledJobView, status_code=status.HTTP_201_CREATED)
async def create_job(
    request: Request,
    body: JobCreateRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ScheduledJobView:
    port = _scheduler(request)

    async def _execute() -> tuple[int, dict]:
        job = await port.create(session, principal=principal, body=body, audit=audit)
        return status.HTTP_201_CREATED, job.model_dump(mode="json")

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
                path="/api/v1/jobs",
                body=body.model_dump(mode="json"),
                execute=_execute,
                # Only the id is kept for replay, so the reminder's words do not
                # outlive a cancel in this table.
                redact_for_storage=lambda response: {"job_id": response["job_id"]},
            )
        except IdempotencyConflict as exc:
            raise AppError(ErrorCode.CONFLICT, str(exc)) from exc
        if "task_reason" in stored.response_body:
            return ScheduledJobView.model_validate(stored.response_body)
        # A replay creates nothing and re-reads through the engine.
        return await port.recall(
            session, principal=principal, job_id=uuid.UUID(stored.response_body["job_id"]), audit=audit
        )

    _status, payload = await _execute()
    return ScheduledJobView.model_validate(payload)


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    request: Request,
    limit: int = 20,
    cursor: str | None = None,
    include_inactive: bool = False,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> JobListResponse:
    if cursor is not None and len(cursor) > MAX_CURSOR_LENGTH:
        raise AppError(ErrorCode.VALIDATION_FAILED, "cursor is too long")
    return await _scheduler(request).list(
        session, principal=principal, limit=max(1, min(limit, MAX_PAGE_SIZE)), cursor=cursor,
        include_inactive=include_inactive, audit=audit,
    )


@router.get("/jobs/{job_id}", response_model=ScheduledJobView)
async def get_job(
    request: Request,
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> ScheduledJobView:
    return await _scheduler(request).recall(session, principal=principal, job_id=job_id, audit=audit)


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_job(
    request: Request,
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
    confirmation_token: str | None = Header(default=None, alias=CONFIRMATION_HEADER, max_length=512),
) -> Response:
    await _scheduler(request).cancel(
        session, principal=principal, job_id=job_id, confirmation_token=confirmation_token, audit=audit
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
