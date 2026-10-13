"""The signed-in user's own view (pilot plan C3/C4/C5, step M2; register §2N).

* `GET /api/v1/me` — who the caller is and what this server has switched on;
* `GET /api/v1/devices` — the caller's own devices (revocation is the
  existing `DELETE /devices/{id}`);
* `GET /api/v1/agent/tasks` — the caller's own task history, newest first.

Read-only and owner-scoped: every query is filtered by the principal resolved
from the bearer token (PHONE-003), so another user's rows are absent, never
refused. Responses carry bounded fields only — no credential handle, push
token, step-up key, confirmation token, task text or task result (a result is
read one task at a time through `GET /agent/tasks/{id}`, which the runtime
owns). Nothing here holds policy: feature flags are the operator's
configuration, reported, never consulted.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config.schema import AppConfig
from server.gateway.deps import get_db_session, get_principal, get_security_core
from server.gateway.errors import AppError
from server.gateway.security import SecurityCore
from server.storage.models import AgentTask, Device, User
from shared.schemas.authorization import Principal
from shared.schemas.enums import DevicePlatform
from shared.schemas.errors import ErrorCode

router = APIRouter(tags=["me"])

MAX_DEVICES = 100
MAX_TASK_PAGE = 100


class FeatureFlags(BaseModel):
    """What the operator switched on (`config.yaml`). Reported so a client can
    hide what does not exist; never an authorization input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory: bool = False
    vault: bool = False
    agents: bool = False
    scheduler: bool = False
    android: bool = False
    evaluation: bool = False
    voice_stt: bool = False
    voice_tts: bool = False


def features_from_config(config: AppConfig | None) -> FeatureFlags:
    if config is None:
        return FeatureFlags()
    return FeatureFlags(
        memory=config.memory.enabled,
        vault=config.vault.enabled,
        agents=config.agents.enabled,
        scheduler=config.scheduler.enabled,
        android=config.android.enabled,
        evaluation=config.evaluation.enabled,
        voice_stt=config.voice.stt is not None,
        voice_tts=config.voice.tts is not None,
    )


class MeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID
    display_name: str | None
    device_id: uuid.UUID
    # Re-checked live against membership (04 §9): a graph the caller has left
    # is reported as none, whatever the session row still says.
    active_graph_id: uuid.UUID | None
    active_devices: int
    features: FeatureFlags


class DeviceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: uuid.UUID
    platform: DevicePlatform
    registered_at: datetime
    last_seen: datetime | None
    revoked: bool
    revoked_at: datetime | None
    step_up_enrolled: bool
    connected: bool
    current: bool


class DeviceListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[DeviceView]


class TaskSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID
    status: str
    mode: str
    failure_code: str | None
    # `device` — started by one of the caller's devices; `delegation` — an
    # unattended agent run under the caller's standing delegation.
    origin: str
    device_id: uuid.UUID | None
    model_calls: int
    tool_calls: int
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class TaskHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[TaskSummary]
    # Pass as `before` for the next (older) page; null on the last page.
    next_before: uuid.UUID | None


@router.get("/me", response_model=MeResponse)
async def me(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    core: SecurityCore = Depends(get_security_core),
) -> MeResponse:
    user = await session.get(User, principal.user_id)
    if user is None:  # the token resolved, so this is an invariant breach — never a 200
        raise AppError(ErrorCode.NOT_FOUND, "not found")
    active_graph = principal.active_graph_id
    if active_graph is not None and not await core.graph_repository.is_active_member(
        session, graph_id=active_graph, user_id=principal.user_id
    ):
        active_graph = None
    devices = (await session.execute(
        select(func.count()).select_from(Device).where(
            Device.user_id == principal.user_id, Device.revoked.is_(False)
        )
    )).scalar_one()
    return MeResponse(
        user_id=principal.user_id,
        display_name=user.display_name,
        device_id=principal.device_id,
        active_graph_id=active_graph,
        active_devices=int(devices),
        features=getattr(request.app.state, "features", None) or FeatureFlags(),
    )


@router.get("/devices", response_model=DeviceListResponse)
async def list_devices(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
) -> DeviceListResponse:
    rows = (await session.execute(
        select(Device)
        .where(Device.user_id == principal.user_id)
        .order_by(Device.registered_at.desc(), Device.device_id.desc())
        .limit(MAX_DEVICES)
    )).scalars().all()
    hub = getattr(request.app.state, "device_hub", None)
    online = hub.connected_device_ids() if hub is not None else frozenset()
    return DeviceListResponse(items=[
        DeviceView(
            device_id=row.device_id,
            platform=row.platform,
            registered_at=row.registered_at,
            last_seen=row.last_seen,
            revoked=row.revoked,
            revoked_at=row.revoked_at,
            step_up_enrolled=row.step_up_public_key is not None,
            connected=row.device_id in online and not row.revoked,
            current=row.device_id == principal.device_id,
        )
        for row in rows
    ])


@router.get("/agent/tasks", response_model=TaskHistoryResponse)
async def task_history(
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    limit: int = Query(default=20, ge=1, le=MAX_TASK_PAGE),
    before: uuid.UUID | None = None,
) -> TaskHistoryResponse:
    mine = AgentTask.user_id == principal.user_id
    query = select(AgentTask).where(mine)
    if before is not None:
        anchor = (await session.execute(
            select(AgentTask.created_at, AgentTask.task_id).where(mine, AgentTask.task_id == before)
        )).first()
        if anchor is None:
            # Another user's task id is as unknown as a made-up one.
            raise AppError(ErrorCode.VALIDATION_FAILED, "unknown cursor")
        query = query.where(or_(
            AgentTask.created_at < anchor.created_at,
            and_(AgentTask.created_at == anchor.created_at, AgentTask.task_id < anchor.task_id),
        ))
    rows = (await session.execute(
        query.order_by(AgentTask.created_at.desc(), AgentTask.task_id.desc()).limit(limit + 1)
    )).scalars().all()
    page = rows[:limit]
    return TaskHistoryResponse(
        items=[
            TaskSummary(
                task_id=row.task_id,
                status=row.status,
                mode=row.mode,
                failure_code=row.failure_code,
                origin="delegation" if row.delegation_id is not None else "device",
                device_id=row.device_id,
                model_calls=row.model_calls,
                tool_calls=row.tool_calls,
                created_at=row.created_at,
                updated_at=row.updated_at,
                finished_at=row.finished_at,
            )
            for row in page
        ],
        next_before=page[-1].task_id if len(rows) > limit else None,
    )
