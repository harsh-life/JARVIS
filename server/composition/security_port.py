"""The runtime's SecurityPort, satisfied by the existing Security Core.

This is the whole bridge between `server.agent` and the deterministic authority.
Every method delegates to an object `build_security_core` already constructed —
there is no authorization logic here of its own:

| port method            | Security Core object                          |
|------------------------|-----------------------------------------------|
| `authorize_action`     | `AuthorizationEngine.authorize` (04 §3)        |
| `authorize_activation` | `AuthorizationEngine.authorize` on a `capability_grant` create |
| `issue_confirmation`   | `ConfirmationService.issue` (PERM-004)         |
| `describe_capability`  | the closed registry + the floor (07 §7)        |
| `holds_standing_grant` | `CapabilityGrantService.has_capability` (D5)   |
| `activate_for_task`    | `CapabilityGrantService.grant` (TASK scope)    |
| `deactivate_task`      | `CapabilityGrantService.revoke`                |
| `invalidate_confirmations` | `ConfirmationService.invalidate_for_task` (18 §5.3) |
| `record`               | `AuditLogger.record` (01 §11.1)                |
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.agent.events import AgentEvent
from server.agent.ports import (
    ActionRequest,
    ActivationRequest,
    CapabilityInfo,
    CapabilityStatus,
    IssuedConfirmation,
    Verdict,
)
from server.capabilities.floor import floor_category_for_capability
from server.capabilities.registry import is_registered, lookup
from server.gateway.security import SecurityCore
from server.graph.authorization import AccessRequest
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.storage.models import CapabilityGrant, Device, Session, User
from shared.schemas.authorization import (
    ActionBinding,
    CapabilityCheckContext,
    DenialSurface,
    Operation,
    Principal,
    ResourceType,
)
from shared.schemas.enums import (
    AuditActor,
    AuditResult,
    CapabilityScopeType,
    PermissionDecisionValue,
    RiskCategory,
    UserStatus,
)

# The runtime's event vocabulary → the audit registry. Exhaustive; an unmapped
# event is a KeyError (a programming error), never a made-up action name.
EVENT_ACTIONS: dict[AgentEvent, AuditAction] = {
    AgentEvent.TASK_SUBMITTED: AuditAction.AGENT_TASK_SUBMITTED,
    AgentEvent.TASK_PAUSED: AuditAction.AGENT_TASK_PAUSED,
    AgentEvent.TASK_COMPLETED: AuditAction.AGENT_TASK_COMPLETED,
    AgentEvent.TASK_FAILED: AuditAction.AGENT_TASK_FAILED,
    AgentEvent.TASK_CANCELLED: AuditAction.AGENT_TASK_CANCELLED,
    AgentEvent.PROPOSAL_REJECTED: AuditAction.AGENT_PROPOSAL_REJECTED,
    AgentEvent.TOOL_EXECUTED: AuditAction.AGENT_TOOL_EXECUTED,
    AgentEvent.TOOL_FAILED: AuditAction.AGENT_TOOL_FAILED,
    AgentEvent.CAPABILITY_ACTIVATED: AuditAction.CAPABILITY_ACTIVATED,
    AgentEvent.CAPABILITY_ACTIVATION_REFUSED: AuditAction.CAPABILITY_ACTIVATION_REFUSED,
    AgentEvent.CAPABILITY_DEACTIVATED: AuditAction.CAPABILITY_DEACTIVATED,
    AgentEvent.CONFIRMATION_ISSUED: AuditAction.CONFIRMATION_ISSUED,
    AgentEvent.CONFIRMATION_ACCEPTED: AuditAction.CONFIRMATION_ACCEPTED,
    AgentEvent.CONFIRMATION_REJECTED: AuditAction.CONFIRMATION_REJECTED,
    AgentEvent.LIMIT_EXCEEDED: AuditAction.USAGE_LIMIT_EXCEEDED,
    AgentEvent.BREAKER_TRIPPED: AuditAction.BREAKER_TRIPPED,
}

# Acts of the human, not the agent.
_USER_EVENTS = frozenset({AgentEvent.CONFIRMATION_ACCEPTED, AgentEvent.CONFIRMATION_REJECTED})
# Acts of the deterministic supervisor, not the agent (18 §5).
_SYSTEM_EVENTS = frozenset({AgentEvent.BREAKER_TRIPPED})

_MAX_RESOURCE = 128


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class RuntimeSecurityAdapter:
    """Request-scoped: one per API call, bound to that call's transaction."""

    def __init__(self, *, core: SecurityCore, session: AsyncSession, audit: AuditLogger) -> None:
        self._core = core
        self._session = session
        self._audit = audit

    # ── authorization ───────────────────────────────────────────────────

    async def authorize_action(self, request: ActionRequest) -> Verdict:
        outcome = await self._core.engine.authorize(
            self._session,
            AccessRequest(
                principal=request.principal,
                operation=request.operation,
                resource_type=request.resource_type,
                resource_ref=request.resource_ref,
                graph_id=request.graph_id,
                required_capability=request.capability,
                capability_operation=request.capability_operation,
                task_id=str(request.task_id),
                arguments=request.arguments,
                confirmation_token=request.confirmation_token,
                resource_scope=request.resource_scope,
            ),
            audit=self._audit,
        )
        return Verdict(
            decision=outcome.decision,
            risk_category=outcome.risk_category,
            reason=outcome.reason,
            prohibited=outcome.surface is DenialSurface.PROHIBITED,
            binding=outcome.confirmation_required_for,
        )

    async def authorize_activation(self, request: ActivationRequest) -> Verdict:
        """A task-scoped grant is a `CapabilityGrant` create — `consequential` by
        the tier table, so the engine answers `require_confirmation`.

        The capability being activated travels in the confirmation binding's
        (hashed) arguments, so a token approves exactly this capability, this
        narrowing, this task. The floor and the registry are checked first: a
        floor capability is `prohibited` and never reaches a confirmation.
        """

        if floor_category_for_capability(request.capability) is not None:
            return Verdict(
                decision=PermissionDecisionValue.DENY,
                risk_category=RiskCategory.HIGH_IRREVERSIBLE,
                reason="prohibited",
                prohibited=True,
            )
        if not is_registered(request.capability):
            return Verdict(PermissionDecisionValue.DENY, RiskCategory.LOW_READ, "unknown_capability")

        outcome = await self._core.engine.authorize(
            self._session,
            AccessRequest(
                principal=request.principal,
                operation=Operation.CREATE,
                resource_type=ResourceType.CAPABILITY_GRANT,
                graph_id=request.graph_id,
                task_id=str(request.task_id),
                arguments={
                    "activate": request.capability,
                    "scope_type": CapabilityScopeType.TASK.value,
                    "resource_scope": dict(request.resource_scope or {}),
                },
                confirmation_token=request.confirmation_token,
            ),
            audit=self._audit,
        )
        return Verdict(
            decision=outcome.decision,
            risk_category=outcome.risk_category,
            reason=outcome.reason,
            prohibited=outcome.surface is DenialSurface.PROHIBITED,
            binding=outcome.confirmation_required_for,
        )

    async def issue_confirmation(
        self, binding: ActionBinding, risk_category: RiskCategory
    ) -> IssuedConfirmation:
        issued = await self._core.confirmations.issue(
            self._session, binding=binding, risk_category=risk_category
        )
        return IssuedConfirmation(token=issued.token, expires_at=_aware(issued.expires_at))

    # ── capabilities ────────────────────────────────────────────────────

    def describe_capability(self, capability: str) -> CapabilityInfo:
        if floor_category_for_capability(capability) is not None:
            return CapabilityInfo(CapabilityStatus.PROHIBITED)
        if not is_registered(capability):
            return CapabilityInfo(CapabilityStatus.UNKNOWN)
        return CapabilityInfo(CapabilityStatus.REGISTERED, frozenset(lookup(capability).scope_keys))

    async def holds_standing_grant(
        self,
        *,
        principal: Principal,
        graph_id: uuid.UUID | None,
        capability: str,
        resource_scope: Mapping[str, str] | None,
    ) -> bool:
        # No task id in the context: only grants the user already made (user,
        # device, session, graph scope) can answer yes here.
        return await self._core.capability_grants.has_capability(
            self._session,
            capability=capability,
            context=CapabilityCheckContext(
                principal=principal, graph_id=graph_id, resource_scope=resource_scope
            ),
        )

    async def activate_for_task(
        self,
        *,
        principal: Principal,
        task_id: uuid.UUID,
        capability: str,
        resource_scope: Mapping[str, str] | None,
        expires_at: datetime,
    ) -> None:
        # granted_by is the user whose approval the engine just verified — the
        # PERM-002 consent act. The grant service re-applies the floor and the
        # registry before writing.
        grant = await self._core.capability_grants.grant(
            self._session,
            principal_id=task_id,
            scope_type=CapabilityScopeType.TASK,
            capability=capability,
            granted_by=principal.user_id,
            resource_scope=resource_scope,
            expires_at=expires_at,
        )
        await self._audit.record(
            actor=AuditActor.USER,
            action=AuditAction.CAPABILITY_GRANTED,
            resource=f"capability_grant:{grant.grant_id}",
            result=AuditResult.SUCCESS,
            user_id=principal.user_id,
            device_id=principal.device_id,
            session_id=principal.session_id,
        )

    async def deactivate_task(self, *, principal: Principal, task_id: uuid.UUID) -> int:
        rows = (
            await self._session.execute(
                select(CapabilityGrant.grant_id).where(
                    CapabilityGrant.principal_id == task_id,
                    CapabilityGrant.scope_type == CapabilityScopeType.TASK,
                    CapabilityGrant.revoked_at.is_(None),
                )
            )
        ).scalars().all()
        for grant_id in rows:
            await self._core.capability_grants.revoke(
                self._session, grant_id=grant_id, revoked_by=principal.user_id
            )
        return len(rows)

    async def invalidate_confirmations(self, *, principal: Principal, task_id: uuid.UUID) -> int:
        return await self._core.confirmations.invalidate_for_task(
            self._session, principal_user_id=principal.user_id, task_id=str(task_id)
        )

    # ── identity freshness ──────────────────────────────────────────────

    async def principal_active(self, principal: Principal) -> bool:
        """The task's own device and session are still valid (SESSION-002:
        revocation is immediate — checked before every step)."""

        device = await self._session.get(Device, principal.device_id)
        if device is None or device.revoked or device.user_id != principal.user_id:
            return False
        # The same link `resolve_principal` checks on every request (02 §1.2):
        # a user suspended or deleted mid-task stops at the next step, not when
        # the task's wall clock runs out.
        user = await self._session.get(User, principal.user_id)
        if user is None or user.status is not UserStatus.ACTIVE:
            return False
        session_row = await self._session.get(Session, principal.session_id)
        if session_row is None or session_row.user_id != principal.user_id:
            return False
        if session_row.device_id != principal.device_id:
            return False
        return _aware(session_row.expires_at) > _utcnow()

    # ── audit ───────────────────────────────────────────────────────────

    async def record(
        self,
        event: AgentEvent,
        *,
        principal: Principal,
        graph_id: uuid.UUID | None,
        resource: str,
        result: AuditResult,
        decision: PermissionDecisionValue | None = None,
    ) -> None:
        await self._audit.record(
            actor=(
                AuditActor.USER if event in _USER_EVENTS
                else AuditActor.SYSTEM if event in _SYSTEM_EVENTS
                else AuditActor.AGENT
            ),
            action=EVENT_ACTIONS[event],
            resource=resource[:_MAX_RESOURCE],
            result=result,
            decision=decision,
            user_id=principal.user_id,
            device_id=principal.device_id,
            session_id=principal.session_id,
            graph_id=graph_id,
        )
