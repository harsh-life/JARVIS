"""The operator console's views — `GET /api/v1/admin/*` (02 §12, 28 §3).

> The dashboard shows. Controls live elsewhere. (28 §0, DASH-002)

| Endpoint | View |
|---|---|
| `GET /admin/banner` | the persistent banners (break-glass enabled, global stop latched) |
| `GET /admin/health` | DB, SecretStore locked/unlocked, model providers, Mem0, vault, scheduler, device channel, Judge |
| `GET /admin/tasks` | counts; per-task status, mode, counters, failure code, worker switches |
| `GET /admin/recovery` | switches, stalls, trips by source, the global latch |
| `GET /admin/break-glass` | enablement, live records, activation/invocation/end events |
| `GET /admin/evaluations` | Judge scores and findings (redacted), the review queue, config versions |
| `GET /admin/usage` | per-user and global spend and rate usage, Judge spend separately |
| `GET /admin/memory` | fact counts per user (no content), vault index state |
| `GET /admin/devices` | registered devices, connected, last seen, revoked |
| `GET /admin/audit` | filterable audit events — ids and results, never values |
| `GET /admin/configuration` | effective config; secrets only as handles + whether they resolve |
| `GET /admin/privileged/tasks/{task_id}?reason=` | **DASH-006**: one task's user content, unredacted — audited before it is returned |

Every route is `GET` (asserted when this module is imported) and depends on
`get_superuser` — an ordinary Bearer token is refused before any handler runs
(DASH-003/004). Every response carries `banners`. Responses are secret-free
and PII-redacted server-side (DASH-005/006). Operator *actions* are the
separate `/admin/control/*` routes, owned by the subsystems that enforce them.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from server.gateway.console_port import OperatorConsolePort
from server.gateway.deps import get_audit_logger, get_db_session
from server.gateway.errors import AppError
from server.gateway.superuser_auth import SuperuserPrincipal, get_superuser
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from shared.schemas.enums import AuditActor, AuditResult
from shared.schemas.errors import ErrorCode
from shared.schemas.evaluation import IDENTIFIER_PATTERN

router = APIRouter(prefix="/admin", tags=["admin-console"])


def _console(request: Request) -> OperatorConsolePort:
    console = getattr(request.app.state, "operator_console", None)
    if console is None:
        raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "the operator console is not configured on this server",
                       details={"dependency": "dashboard"})
    return console


async def _page(request: Request, session: AsyncSession, view: str, data: dict) -> dict:
    return {"view": view, "banners": await _console(request).banners(session), "data": data}


@router.get("/banner")
async def banner(request: Request, _: SuperuserPrincipal = Depends(get_superuser),
                 session: AsyncSession = Depends(get_db_session)) -> dict:
    return {"banners": await _console(request).banners(session)}


@router.get("/health")
async def health(request: Request, probe_models: bool = False, _: SuperuserPrincipal = Depends(get_superuser),
                 session: AsyncSession = Depends(get_db_session)) -> dict:
    return await _page(request, session, "health",
                       await _console(request).health(session, probe_models=probe_models))


@router.get("/tasks")
async def tasks(request: Request, limit: int = Query(100, ge=1, le=500), status: str | None = None,
                _: SuperuserPrincipal = Depends(get_superuser),
                session: AsyncSession = Depends(get_db_session)) -> dict:
    return await _page(request, session, "tasks", await _console(request).tasks(session, limit=limit, status=status))


@router.get("/recovery")
async def recovery(request: Request, limit: int = Query(100, ge=1, le=500),
                   _: SuperuserPrincipal = Depends(get_superuser),
                   session: AsyncSession = Depends(get_db_session)) -> dict:
    return await _page(request, session, "recovery", await _console(request).recovery(session, limit=limit))


@router.get("/break-glass")
async def break_glass(request: Request, limit: int = Query(100, ge=1, le=500),
                      _: SuperuserPrincipal = Depends(get_superuser),
                      session: AsyncSession = Depends(get_db_session)) -> dict:
    return await _page(request, session, "break_glass", await _console(request).break_glass(session, limit=limit))


@router.get("/evaluations")
async def evaluations(request: Request, limit: int = Query(100, ge=1, le=500),
                      _: SuperuserPrincipal = Depends(get_superuser),
                      session: AsyncSession = Depends(get_db_session)) -> dict:
    return await _page(request, session, "evaluations", await _console(request).evaluations(session, limit=limit))


@router.get("/usage")
async def usage(request: Request, _: SuperuserPrincipal = Depends(get_superuser),
                session: AsyncSession = Depends(get_db_session)) -> dict:
    return await _page(request, session, "usage", await _console(request).usage(session))


@router.get("/memory")
async def memory(request: Request, _: SuperuserPrincipal = Depends(get_superuser),
                 session: AsyncSession = Depends(get_db_session)) -> dict:
    return await _page(request, session, "memory", await _console(request).memory(session))


@router.get("/devices")
async def devices(request: Request, limit: int = Query(200, ge=1, le=500),
                  _: SuperuserPrincipal = Depends(get_superuser),
                  session: AsyncSession = Depends(get_db_session)) -> dict:
    return await _page(request, session, "devices", await _console(request).devices(session, limit=limit))


@router.get("/audit")
async def audit_events(
    request: Request,
    action: str | None = Query(None, max_length=64),
    user_id: uuid.UUID | None = None,
    result: str | None = Query(None, pattern=r"^(success|failure|blocked)$"),
    resource_prefix: str | None = Query(None, max_length=64),
    since: datetime | None = None,
    limit: int = Query(200, ge=1, le=500),
    _: SuperuserPrincipal = Depends(get_superuser),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    return await _page(request, session, "audit", await _console(request).audit(
        session, action=action, user_id=user_id, result=result, resource_prefix=resource_prefix,
        since=since, limit=limit,
    ))


@router.get("/configuration")
async def configuration(request: Request, _: SuperuserPrincipal = Depends(get_superuser),
                        session: AsyncSession = Depends(get_db_session)) -> dict:
    return await _page(request, session, "configuration", await _console(request).configuration(session))


@router.get("/privileged/tasks/{task_id}")
async def unredacted_task(
    task_id: uuid.UUID,
    request: Request,
    reason: str = Query(..., pattern=IDENTIFIER_PATTERN),
    _: SuperuserPrincipal = Depends(get_superuser),
    session: AsyncSession = Depends(get_db_session),
    audit: AuditLogger = Depends(get_audit_logger),
) -> dict:
    """DASH-006: the separate, privileged, audited view of one task's user
    content. The access is written to the audit trail — with its reason —
    before anything is read, so no unredacted view goes unrecorded."""

    console = _console(request)
    await audit.record(actor=AuditActor.SUPERUSER, action=AuditAction.CONSOLE_UNREDACTED_VIEW,
                       resource=f"console:unredacted:task:{task_id}:{reason}", result=AuditResult.SUCCESS)
    content = await console.task_content(session, task_id)
    if content is None:
        raise AppError(ErrorCode.NOT_FOUND, "no such task")
    return await _page(request, session, "task_content", content)


def _assert_read_only(api: APIRouter) -> None:
    for route in api.routes:
        methods = set(getattr(route, "methods", None) or ())
        if methods - {"GET", "HEAD"}:
            raise RuntimeError(f"operator console route {route.path} is not read-only: {sorted(methods)}")


# DASH-002 / DSH-T1 at import time: a non-GET route here is a startup failure.
_assert_read_only(router)
