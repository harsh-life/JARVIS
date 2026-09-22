"""Agent Runtime endpoints — 02_API_PROTOCOL.md §5.

`[LOCKED]` (02 §5): "The agent endpoint returns a **proposal-executed-under-
authorization** result, never raw model execution authority (P1)." Every
handler below does exactly one thing beyond translating HTTP<->the
orchestrator: it constructs a fresh, per-request `AgentOrchestrator` (via
`server.gateway.runtime.build_agent_orchestrator`) bound to the *authenticated*
principal from `get_principal` — never a `user_id` read from the request body
— and maps the returned `AgentResult`/raised exception onto 02 §1.7's error
codes. No handler here authorizes, confirms, or dispatches anything itself.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from server.agent.tasks import TaskConflict, UnknownTask
from server.config.schema import AppConfig
from server.gateway.deps import (
    get_app_config,
    get_audit_logger,
    get_db_session,
    get_principal,
    get_runtime_core,
    get_security_core,
)
from server.gateway.errors import AppError
from server.gateway.runtime import RuntimeCore, build_agent_orchestrator
from server.gateway.security import SecurityCore
from server.security.audit import AuditLogger
from shared.schemas.authorization import Principal
from shared.schemas.errors import ErrorCode
from shared.schemas.runtime import AgentRequest, AgentResult, ConfirmRequest, FailureReason, TaskStatus

router = APIRouter(tags=["agent"])

_BOUND_BREACH_REASONS = frozenset(
    {
        FailureReason.RUNAWAY_ITERATIONS,
        FailureReason.RUNAWAY_TOOL_CALLS,
        FailureReason.RUNAWAY_MODEL_CALLS,
        FailureReason.TIMEOUT,
        FailureReason.BUDGET_EXCEEDED,
    }
)


def _raise_for_terminal_result(result: AgentResult) -> None:
    """02 §5's documented error table for this endpoint family, applied
    uniformly across create/confirm: `confirmation_required` (403), a bound
    breach (429, RATE-001/FAIL-CORE-001), or a model outage (503, FAIL-005).
    Every other outcome — `completed`, `cancelled`, or a `failed` for any
    other reason (malformed proposal, context-assembly failure) — is returned
    as an ordinary `200 AgentResult`; it is still an *explicit* result (never
    a silent hang), just not one 02 §1.7 asks to be a distinct HTTP error.
    """

    if result.status is TaskStatus.AWAITING_CONFIRMATION:
        raise AppError(
            ErrorCode.CONFIRMATION_REQUIRED,
            "this action requires human confirmation before it may proceed",
            details={"task_id": result.task_id, "confirmation_token": result.confirmation_token},
        )
    if result.status is TaskStatus.FAILED and result.failure_reason in _BOUND_BREACH_REASONS:
        raise AppError(
            ErrorCode.RATE_LIMITED,
            f"task stopped: {result.failure_reason.value}",
            details={"task_id": result.task_id},
        )
    if result.status is TaskStatus.FAILED and result.failure_reason is FailureReason.MODEL_UNAVAILABLE:
        raise AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "the configured model is unavailable",
            details={"task_id": result.task_id},
            retryable=True,
        )


async def _orchestrator(
    session: AsyncSession,
    audit: AuditLogger,
    principal: Principal,
    core: SecurityCore,
    runtime: RuntimeCore,
    app_config: AppConfig,
):
    return await build_agent_orchestrator(
        session=session,
        audit=audit,
        principal=principal,
        security=core,
        runtime=runtime,
        app_config=app_config,
    )


@router.post("/agent/tasks", response_model=AgentResult)
async def create_task(
    body: AgentRequest,
    session: AsyncSession = Depends(get_db_session),
    audit: AuditLogger = Depends(get_audit_logger),
    principal: Principal = Depends(get_principal),
    core: SecurityCore = Depends(get_security_core),
    runtime: RuntimeCore = Depends(get_runtime_core),
    app_config: AppConfig = Depends(get_app_config),
) -> AgentResult:
    orchestrator = await _orchestrator(session, audit, principal, core, runtime, app_config)
    result = await orchestrator.start(body.input)
    _raise_for_terminal_result(result)
    return result


@router.post("/agent/tasks/{task_id}/confirm", response_model=AgentResult)
async def confirm_task(
    task_id: str,
    body: ConfirmRequest,
    session: AsyncSession = Depends(get_db_session),
    audit: AuditLogger = Depends(get_audit_logger),
    principal: Principal = Depends(get_principal),
    core: SecurityCore = Depends(get_security_core),
    runtime: RuntimeCore = Depends(get_runtime_core),
    app_config: AppConfig = Depends(get_app_config),
) -> AgentResult:
    """05 §4 — `[LOCKED]` no timeout auto-approves; a task not currently
    `awaiting_confirmation` is a `409 conflict`, not silently accepted."""

    orchestrator = await _orchestrator(session, audit, principal, core, runtime, app_config)
    try:
        result = await orchestrator.resume(
            task_id, confirmation_token=body.confirmation_token, approve=body.approve
        )
    except UnknownTask as exc:
        raise AppError(ErrorCode.NOT_FOUND, "not found") from exc
    except TaskConflict as exc:
        raise AppError(ErrorCode.CONFLICT, str(exc)) from exc

    _raise_for_terminal_result(result)
    return result


@router.post("/agent/tasks/{task_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_task(
    task_id: str,
    principal: Principal = Depends(get_principal),
    runtime: RuntimeCore = Depends(get_runtime_core),
) -> Response:
    """No `AgentOrchestrator` is constructed here — cancellation touches only
    `TaskStore` (05 §9), not the authorization/model/tool ports a running
    loop needs, so this handler stays minimal on purpose."""

    try:
        await runtime.task_store.request_cancellation(task_id, requested_by=principal)
    except UnknownTask as exc:
        raise AppError(ErrorCode.NOT_FOUND, "not found") from exc
    except TaskConflict as exc:
        raise AppError(ErrorCode.CONFLICT, str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/agent/tasks/{task_id}", response_model=AgentResult)
async def get_task(
    task_id: str,
    principal: Principal = Depends(get_principal),
    runtime: RuntimeCore = Depends(get_runtime_core),
) -> AgentResult:
    try:
        state_or_result = await runtime.task_store.get(task_id)
    except UnknownTask as exc:
        raise AppError(ErrorCode.NOT_FOUND, "not found") from exc

    if state_or_result.principal.user_id != principal.user_id:
        # 04 §7's anti-enumeration posture, applied to a task_id: another
        # user's task is reported absent, never "forbidden".
        raise AppError(ErrorCode.NOT_FOUND, "not found")
    if state_or_result.result is not None:
        return state_or_result.result
    return AgentResult(
        task_id=state_or_result.task_id,
        status=state_or_result.status,
        iterations_used=state_or_result.iterations,
        tool_calls_used=state_or_result.tool_calls,
        model_calls_used=state_or_result.model_calls,
    )
