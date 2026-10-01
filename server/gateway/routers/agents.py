"""Agent Factory endpoints — docs/29 §23.2, Phase 1.

`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]` (docs/29). Owner-only, `02`
conventions:

* `POST /agents/compile` takes a draft (semantic intent only) and returns a
  `CompileOutcome`: a single-use, owner-bound, expiring preview with the
  deterministic confirmation card, a clarification, or a refusal. The body is
  read as plain JSON and validated by the factory, so a malformed draft is a
  `422` that names fields and never echoes a value.
* `POST /agents` / `PATCH /agents/{id}` redeem a preview. Creating or changing
  an agent is `consequential`: the first call answers `403
  confirmation_required` with a token bound to exactly that preview, and the
  retry with `X-Confirmation-Token` applies it.
* `DELETE /agents/{id}` likewise needs confirmation.
* `POST /agents/{id}/runs` (Phase 2) runs the agent now, as an ordinary task
  of the present owner, in the spec's mode, and answers with the run and its
  task (`202`: a run paused for confirmation is confirmed like any task). The
  body is empty: nothing about the run is the request's to name.
* `GET /agents/inbox` (owner), `POST /agents/inbox/{item_id}/read`, `DELETE
  /agents/inbox/{item_id}`: each finished run's result, delivered to its
  owner only, as plain bounded text.
* `POST /agents/{id}/runs/{run_id}/cancel` stops a run and `POST
  /agents/{id}/pause` pauses the agent and stops its live runs — the safe
  direction, never confirmed. `POST /agents/{id}/resume` gives authority back:
  it is re-checked and confirmed like a change to the agent.
* Another user's agent or preview is `404`, indistinguishable from an absent
  one (04 §7, AGENT-T9).

Without a configured factory (`agents.enabled: false`) every endpoint answers
`503 dependency_unavailable` with `dependency: agents`.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from server.gateway.agents_port import AgentFactoryPort
from server.gateway.deps import get_audit_logger, get_db_session, get_principal
from server.gateway.errors import AppError
from server.security.audit import AuditLogger
from shared.schemas.agent_factory import (
    AgentDetail,
    AgentInboxItemView,
    AgentInboxResponse,
    AgentListResponse,
    AgentRunListResponse,
    AgentRunView,
    AgentView,
    CompiledAgentSpecView,
    CompileOutcome,
    CreateAgentRequest,
    NotebookResponse,
    RunAgentRequest,
)
from shared.schemas.authorization import Principal
from shared.schemas.errors import ErrorCode

router = APIRouter(tags=["agents"])

CONFIRMATION_HEADER = "X-Confirmation-Token"


def _factory(request: Request) -> AgentFactoryPort:
    port = getattr(request.app.state, "agent_factory", None)
    if port is None:
        raise AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "agents are not available on this server",
            details={"dependency": "agents"},
        )
    return port


@router.post("/agents/compile", response_model=CompileOutcome)
async def compile_agent(
    request: Request,
    draft: Any = Body(...),
    agent_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> CompileOutcome:
    return await _factory(request).compile(session, principal=principal, draft=draft, agent_id=agent_id, audit=audit)


@router.get("/agents/previews/{compile_id}", response_model=CompiledAgentSpecView)
async def get_preview(
    request: Request,
    compile_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> CompiledAgentSpecView:
    """The card for one of the caller's own pending previews — what a device
    renders when a task pauses on `agent.define.create`/`update`."""

    return await _factory(request).preview(session, principal=principal, compile_id=compile_id, audit=audit)


@router.post("/agents", response_model=AgentView, status_code=status.HTTP_201_CREATED)
async def create_agent(
    request: Request,
    body: CreateAgentRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
    confirmation_token: str | None = Header(default=None, alias=CONFIRMATION_HEADER, max_length=512),
) -> AgentView:
    return await _factory(request).create(session, principal=principal, body=body,
                                          confirmation_token=confirmation_token, audit=audit)


@router.get("/agents", response_model=AgentListResponse)
async def list_agents(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> AgentListResponse:
    return await _factory(request).list(session, principal=principal, audit=audit)


# Declared before `/agents/{agent_id}` so `inbox` is never read as an agent id.
@router.get("/agents/inbox", response_model=AgentInboxResponse)
async def get_inbox(
    request: Request,
    agent_id: uuid.UUID | None = None,
    unread: bool = False,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> AgentInboxResponse:
    return await _factory(request).inbox(session, principal=principal, agent_id=agent_id, unread=unread,
                                         audit=audit)


@router.post("/agents/inbox/{item_id}/read", response_model=AgentInboxItemView)
async def mark_inbox_read(
    request: Request,
    item_id: uuid.UUID,
    body: RunAgentRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> AgentInboxItemView:
    return await _factory(request).mark_inbox_read(session, principal=principal, item_id=item_id, audit=audit)


@router.delete("/agents/inbox/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_inbox_item(
    request: Request,
    item_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> Response:
    await _factory(request).delete_inbox_item(session, principal=principal, item_id=item_id, audit=audit)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/agents/{agent_id}", response_model=AgentDetail)
async def get_agent(
    request: Request,
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> AgentDetail:
    return await _factory(request).get(session, principal=principal, agent_id=agent_id, audit=audit)


@router.patch("/agents/{agent_id}", response_model=AgentView)
async def update_agent(
    request: Request,
    agent_id: uuid.UUID,
    body: CreateAgentRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
    confirmation_token: str | None = Header(default=None, alias=CONFIRMATION_HEADER, max_length=512),
) -> AgentView:
    return await _factory(request).update(session, principal=principal, agent_id=agent_id, body=body,
                                          confirmation_token=confirmation_token, audit=audit)


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    request: Request,
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
    confirmation_token: str | None = Header(default=None, alias=CONFIRMATION_HEADER, max_length=512),
) -> Response:
    await _factory(request).delete(session, principal=principal, agent_id=agent_id,
                                   confirmation_token=confirmation_token, audit=audit)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/agents/{agent_id}/runs", response_model=AgentRunView, status_code=status.HTTP_202_ACCEPTED)
async def run_agent(
    request: Request,
    agent_id: uuid.UUID,
    body: RunAgentRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> AgentRunView:
    return await _factory(request).run(session, principal=principal, agent_id=agent_id, audit=audit)


@router.get("/agents/{agent_id}/runs", response_model=AgentRunListResponse)
async def list_runs(
    request: Request,
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> AgentRunListResponse:
    return await _factory(request).list_runs(session, principal=principal, agent_id=agent_id, audit=audit)


@router.get("/agents/{agent_id}/runs/{run_id}", response_model=AgentRunView)
async def get_run(
    request: Request,
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> AgentRunView:
    return await _factory(request).get_run(session, principal=principal, agent_id=agent_id, run_id=run_id,
                                           audit=audit)


@router.post("/agents/{agent_id}/runs/{run_id}/cancel", response_model=AgentRunView)
async def cancel_run(
    request: Request,
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    body: RunAgentRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> AgentRunView:
    return await _factory(request).cancel_run(session, principal=principal, agent_id=agent_id, run_id=run_id,
                                              audit=audit)


@router.post("/agents/{agent_id}/pause", response_model=AgentView)
async def pause_agent(
    request: Request,
    agent_id: uuid.UUID,
    body: RunAgentRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> AgentView:
    return await _factory(request).pause(session, principal=principal, agent_id=agent_id, audit=audit)


@router.post("/agents/{agent_id}/resume", response_model=AgentView)
async def resume_agent(
    request: Request,
    agent_id: uuid.UUID,
    body: RunAgentRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
    confirmation_token: str | None = Header(default=None, alias=CONFIRMATION_HEADER, max_length=512),
) -> AgentView:
    return await _factory(request).resume(session, principal=principal, agent_id=agent_id,
                                          confirmation_token=confirmation_token, audit=audit)


@router.get("/agents/{agent_id}/notebook", response_model=NotebookResponse)
async def get_notebook(
    request: Request,
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> NotebookResponse:
    return await _factory(request).notebook(session, principal=principal, agent_id=agent_id, audit=audit)


@router.delete("/agents/{agent_id}/notebook", status_code=status.HTTP_204_NO_CONTENT)
async def clear_notebook(
    request: Request,
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> Response:
    await _factory(request).clear_notebook(session, principal=principal, agent_id=agent_id, audit=audit)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
