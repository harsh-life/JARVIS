"""The audit writer (01 §11.1, 02 §2 step 8, 04 §3, 12 §5).

`AuditEvent` is append-only. That is enforced structurally here rather than by
convention: this module exposes `record()` and nothing else — no update path,
no delete path, no "correct an earlier event" helper. PERM-006's "the agent
cannot disable or write false audit entries" is then a property of the module
surface, not of reviewer vigilance.

`[LOCKED]` (SECRET-004, 12 §6) nothing written here can carry a secret:
- `action` must be a member of `AuditAction` (server/security/events.py) — a
  caller cannot smuggle data through a free-form action name;
- `resource` is a short structured reference (`"<type>:<id>"`), length-capped;
- `AuditEvent` has no payload/details column at all.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from server.secrets.audit_port import SecretAuditEvent
from server.security.events import SECRET_ACTION_BY_NAME, AuditAction
from server.storage.models import AuditEvent, PermissionDecision
from shared.schemas.enums import (
    AuditActor,
    AuditResult,
    PermissionDecisionValue,
    RiskCategory,
)

logger = logging.getLogger("hypermind.security.audit")

# A resource reference is "<resource_type>:<uuid>" or a short symbolic name.
# Capped so that no caller can turn this column into a general-purpose
# payload field (which is how secrets end up in audit logs).
MAX_RESOURCE_LENGTH = 128


class AuditLogger:
    """Per-request audit writer.

    Holds the request's correlation id so every event, permission decision,
    and usage record for one request threads together (02 §1.3). One instance
    per request; the gateway builds it in a dependency.
    """

    def __init__(self, session: AsyncSession, *, request_id: uuid.UUID) -> None:
        self._session = session
        self._request_id = request_id

    @property
    def request_id(self) -> uuid.UUID:
        return self._request_id

    async def record(
        self,
        *,
        actor: AuditActor,
        action: AuditAction,
        resource: str,
        result: AuditResult,
        decision: PermissionDecisionValue | None = None,
        user_id: uuid.UUID | None = None,
        device_id: uuid.UUID | None = None,
        session_id: uuid.UUID | None = None,
        graph_id: uuid.UUID | None = None,
    ) -> None:
        if not isinstance(action, AuditAction):
            # Fail loudly rather than inventing an action name: an
            # unregistered action would be invisible to every query 17's
            # acceptance tests run.
            raise ValueError(
                "audit action must be a member of AuditAction "
                "(server/security/events.py) — see 01 §11.1"
            )

        self._session.add(
            AuditEvent(
                request_id=self._request_id,
                user_id=user_id,
                device_id=device_id,
                session_id=session_id,
                graph_id=graph_id,
                actor=actor,
                action=action.value,
                resource=_safe_resource(resource),
                decision=decision,
                result=result,
                timestamp=_utcnow(),
            )
        )
        await self._session.flush()

    async def record_permission_decision(
        self,
        *,
        principal_id: uuid.UUID,
        capability: str | None,
        resource_ref: str,
        decision: PermissionDecisionValue,
        risk_category: RiskCategory,
        reason: str,
    ) -> None:
        """04 §1: "Every call produces a `PermissionDecision` and an
        `AuditEvent`." This writes the former; the engine writes both.

        `capability` is nullable on an access request that needs none, but the
        column is not — the empty string records "no capability was required"
        distinguishably from a capability named `""`, which the registry
        cannot contain.
        """

        self._session.add(
            PermissionDecision(
                request_id=self._request_id,
                principal_id=principal_id,
                capability=capability or "",
                resource_ref=_safe_resource(resource_ref),
                decision=decision,
                risk_category=risk_category,
                reason=reason,
                timestamp=_utcnow(),
            )
        )
        await self._session.flush()

    async def record_secret_event(self, event: SecretAuditEvent) -> None:
        """Satisfies `server.secrets.audit_port.SecretAuditSink` structurally.

        The store speaks plain action strings because it sits in a lower layer
        and cannot import `AuditAction` (16 §2); this is where that vocabulary
        is translated. `event` carries the handle and the requester
        description — never a value (12 §5).
        """

        action = SECRET_ACTION_BY_NAME.get(event.action)
        if action is None:
            raise ValueError(f"unmapped secret audit action: {event.action!r}")

        actor = _ACTOR_BY_REQUESTER_PREFIX.get(event.requester.split(":", 1)[0], AuditActor.SYSTEM)

        await self.record(
            actor=actor,
            action=action,
            resource=event.secret_ref,
            result=event.result,
        )


def _safe_resource(resource: str) -> str:
    """Keep the resource column a *reference*, never a payload."""

    collapsed = " ".join(resource.split())
    if len(collapsed) > MAX_RESOURCE_LENGTH:
        return collapsed[:MAX_RESOURCE_LENGTH]
    return collapsed


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_ACTOR_BY_REQUESTER_PREFIX: dict[str, AuditActor] = {
    "agent": AuditActor.AGENT,
    "tool": AuditActor.TOOL,
    "user": AuditActor.USER,
    "server": AuditActor.SYSTEM,
    "superuser": AuditActor.SUPERUSER,
}
