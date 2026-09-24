"""Agent endpoints — 02 §5, plus `GET /config/tools` (§6) and
`GET /intelligence/status` (§11).

`[LOCKED]` (02 §5) the agent endpoint returns a *proposal-executed-under-
authorization* result, never raw model execution authority. A plan that reaches
a consequential action returns `403 confirmation_required` with the token and
the explicit operation details, and nothing further happens until `/confirm`.
An absolute-floor action is never surfaced as confirmable.

Identity comes only from the bearer token (`get_principal` /
`get_resolved_session`); no route here reads a user, device, session, or graph
id from the body (PHONE-003).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.errors import StepUpRequired
from server.auth.sessions import ResolvedSession
from server.gateway.agent_port import AgentTaskPort
from server.gateway.deps import (
    get_audit_logger,
    get_db_session,
    get_resolved_session,
    get_security_core,
)
from server.gateway.errors import AppError
from server.gateway.security import SecurityCore
from server.security.audit import AuditLogger
from server.storage.idempotency import IdempotencyConflict, get_or_execute
from shared.schemas.agent import AgentFailureCode, AgentResult, AgentTaskStatus, TaskMode, ToolSummary
from shared.schemas.errors import ERROR_CODE_TABLE, ErrorCode

router = APIRouter(tags=["agent"])

# Failure → 02 §1.7 code. 02 §5 names 429 for "loop/budget" and 503 for
# "model down"; the rest follow the error table's meanings.
_FAILURE_CODES: dict[AgentFailureCode, ErrorCode] = {
    AgentFailureCode.MAX_ITERATIONS: ErrorCode.RATE_LIMITED,
    AgentFailureCode.MAX_MODEL_CALLS: ErrorCode.RATE_LIMITED,
    AgentFailureCode.MAX_TOOL_CALLS: ErrorCode.RATE_LIMITED,
    AgentFailureCode.TIMEOUT: ErrorCode.RATE_LIMITED,
    AgentFailureCode.BUDGET_EXCEEDED: ErrorCode.RATE_LIMITED,
    AgentFailureCode.RATE_LIMITED: ErrorCode.RATE_LIMITED,
    AgentFailureCode.MODEL_UNAVAILABLE: ErrorCode.DEPENDENCY_UNAVAILABLE,
    AgentFailureCode.UNPARSEABLE_PROPOSAL: ErrorCode.DEPENDENCY_UNAVAILABLE,
    AgentFailureCode.CONFIRMATION_EXPIRED: ErrorCode.CONFLICT,
    AgentFailureCode.CONFIRMATION_STATE_LOST: ErrorCode.CONFLICT,
    AgentFailureCode.PRINCIPAL_REVOKED: ErrorCode.UNAUTHENTICATED,
    AgentFailureCode.INTERNAL_ERROR: ErrorCode.INTERNAL_ERROR,
    # 18 §5/§6: the breaker stopped the task. Terminal and not retryable — the
    # task is never resumed; a new task is the only way on. `failure_code` in
    # the details distinguishes it from other conflicts.
    AgentFailureCode.EMERGENCY_STOP: ErrorCode.CONFLICT,
}


class SubmitTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: str = Field(min_length=1, max_length=20_000)
    stream: bool = False  # 02 §1.9: non-streaming is the MVP default
    # 18 §3: chosen by the caller at submission, immutable afterwards; the
    # worker never sets it. `execute` is today's behaviour.
    mode: TaskMode = TaskMode.EXECUTE


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_token: str = Field(min_length=1, max_length=512)
    approve: bool


def _runtime(request: Request) -> AgentTaskPort:
    runtime = getattr(request.app.state, "agent_tasks", None)
    if runtime is None:
        raise AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "the agent runtime is not configured on this server",
            details={"dependency": "agent_runtime"},
        )
    return runtime


def render(result: AgentResult, request_id: uuid.UUID | str) -> tuple[int, dict]:
    """An `AgentResult` as (status, body). Success is the result itself; a pause
    or a failure is the canonical error envelope (02 §1.6) with the task id."""

    body = result.model_dump(mode="json")
    if result.status is AgentTaskStatus.AWAITING_CONFIRMATION and result.pending is not None:
        code = ErrorCode.CONFIRMATION_REQUIRED
        message = "this action needs your confirmation before it runs"
        details = {
            "task_id": body["task_id"],
            "confirmation_token": result.pending.confirmation_token,
            "pending": body["pending"],
        }
    elif result.status is AgentTaskStatus.FAILED and result.failure is not None:
        code = _FAILURE_CODES[result.failure.code]
        message = result.failure.message
        details = {"task_id": body["task_id"], "failure_code": result.failure.code.value}
        if code is ErrorCode.DEPENDENCY_UNAVAILABLE:
            details["dependency"] = "model"
        if code is ErrorCode.RATE_LIMITED:
            details["retry_after"] = 60
    else:
        return 200, body

    status, retryable = ERROR_CODE_TABLE[code]
    return status, {
        "error": {
            "code": code.value,
            "message": message,
            "request_id": str(request_id),
            "retryable": retryable,
            "details": details,
        }
    }


def _without_confirmation_token(body: dict) -> dict:
    """The replay copy of a paused-task response, minus its confirmation token.

    Only the token's hash is persisted (`ConfirmationToken`); a plaintext copy in
    the idempotency table would undo that. A client retrying after a lost
    response still gets the task id and the pending action, and fetches the
    token from `GET /agent/tasks/{id}` — owner-only, like the confirmation.
    """

    import copy

    stored = copy.deepcopy(body)
    details = stored.get("error", {}).get("details")
    if isinstance(details, dict) and "confirmation_token" in details:
        details["confirmation_token"] = None
        pending = details.get("pending")
        if isinstance(pending, dict) and "confirmation_token" in pending:
            pending["confirmation_token"] = None
    return stored


@router.post("/agent/tasks")
async def submit_task(
    body: SubmitTaskRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: AsyncSession = Depends(get_db_session),
    resolved: ResolvedSession = Depends(get_resolved_session),
    audit: AuditLogger = Depends(get_audit_logger),
) -> JSONResponse:
    """02 §5. `Idempotency-Key` is required (02 §1.4): a retried submission
    returns the original result and never runs the task twice.

    The key is namespaced by the authenticated user, so one user's key can never
    replay another user's stored result.
    """

    if not idempotency_key or len(idempotency_key) > 200:
        raise AppError(ErrorCode.VALIDATION_FAILED, "Idempotency-Key header is required")
    runtime = _runtime(request)
    principal = resolved.principal

    async def execute() -> tuple[int, dict]:
        result = await runtime.submit(
            session, principal=principal, user_input=body.input, audit=audit, mode=body.mode
        )
        return render(result, audit.request_id)

    try:
        stored = await get_or_execute(
            session,
            idempotency_key=f"{principal.user_id}:{idempotency_key}",
            method="POST",
            path="/api/v1/agent/tasks",
            body=body.model_dump(mode="json"),
            execute=execute,
            redact_for_storage=_without_confirmation_token,
        )
    except IdempotencyConflict:
        raise AppError(ErrorCode.CONFLICT, "Idempotency-Key reused for a different request") from None
    return JSONResponse(status_code=stored.status_code, content=stored.response_body)


@router.post("/agent/tasks/{task_id}/confirm")
async def confirm_task(
    task_id: uuid.UUID,
    body: ConfirmRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    core: SecurityCore = Depends(get_security_core),
    resolved: ResolvedSession = Depends(get_resolved_session),
    audit: AuditLogger = Depends(get_audit_logger),
) -> JSONResponse:
    """02 §5 / PERM-004. Approving a `high_irreversible` action additionally
    needs a step-up-fresh session (OD-F1 tier 4, SESSION-003); the freshness
    fact is computed here from the token and enforced by the runtime."""

    try:
        core.sessions.require_step_up(resolved)
        step_up_fresh = True
    except StepUpRequired:
        step_up_fresh = False

    result = await _runtime(request).confirm(
        session,
        principal=resolved.principal,
        task_id=task_id,
        confirmation_token=body.confirmation_token,
        approve=body.approve,
        step_up_fresh=step_up_fresh,
        audit=audit,
    )
    status, payload = render(result, audit.request_id)
    return JSONResponse(status_code=status, content=payload)


@router.post("/agent/tasks/{task_id}/cancel", response_model=AgentResult)
async def cancel_task(
    task_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    resolved: ResolvedSession = Depends(get_resolved_session),
    audit: AuditLogger = Depends(get_audit_logger),
) -> AgentResult:
    return await _runtime(request).cancel(
        session, principal=resolved.principal, task_id=task_id, audit=audit
    )


@router.get("/agent/tasks/{task_id}", response_model=AgentResult)
async def get_task(
    task_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    resolved: ResolvedSession = Depends(get_resolved_session),
    audit: AuditLogger = Depends(get_audit_logger),
) -> AgentResult:
    """Owner-only (any of the owner's devices). Another user's task is `404`,
    indistinguishable from an absent one (04 §7)."""

    return await _runtime(request).get(
        session, principal=resolved.principal, task_id=task_id, audit=audit
    )


class ToolListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ToolSummary]


@router.get("/config/tools", response_model=ToolListResponse)
async def list_tools(
    request: Request, resolved: ResolvedSession = Depends(get_resolved_session)
) -> ToolListResponse:
    return ToolListResponse(items=_runtime(request).tool_summaries())


@router.get("/intelligence/status")
async def intelligence_status(
    request: Request, resolved: ResolvedSession = Depends(get_resolved_session)
) -> dict:
    """02 §11 / INTEL-003. No query/execute intelligence endpoint exists while
    disabled — the capability is absent, not present-but-erroring (P3)."""

    return {"enabled": bool(getattr(request.app.state, "intelligence_enabled", False))}
