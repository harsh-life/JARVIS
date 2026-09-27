"""What the Judge's control routes need (19 §9, 28 §1) — a Protocol, satisfied by
the composition root (`server/composition/improvements.py`).

Controls live with the subsystem that enforces them, not in the dashboard
(28 §0): switching the Judge (or its stop requests) off and on, and deciding
the improvement review queue — approve, reject, roll back. Every method takes a
`SuperuserPrincipal`, the output of `get_superuser`; there is no user, device,
model, tool or evaluator identity anywhere in this interface, so nothing but an
authenticated operator can approve a candidate or turn a switch.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from server.gateway.superuser_auth import SuperuserPrincipal
from server.security.audit import AuditLogger


class EvaluationControlRefusalKind(str, Enum):
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"
    VALIDATION = "validation"


class EvaluationControlRefused(Exception):
    def __init__(self, kind: EvaluationControlRefusalKind, code: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code


@dataclass(frozen=True)
class SwitchesView:
    configured_enabled: bool
    configured_may_request_stop: bool
    judge_enabled: bool
    stop_requests_enabled: bool


@dataclass(frozen=True)
class DecisionView:
    candidate_id: uuid.UUID
    status: str
    config_version_id: int | None
    target_key: str


@dataclass(frozen=True)
class VersionView:
    version_id: int
    target_key: str
    action: str
    rolled_back_version_id: int | None


class EvaluationControlPort(Protocol):
    async def set_switches(
        self, session: AsyncSession, audit: AuditLogger, *, principal: SuperuserPrincipal,
        judge_enabled: bool | None, stop_requests_enabled: bool | None, reason: str,
    ) -> SwitchesView: ...

    async def approve_candidate(
        self, session: AsyncSession, audit: AuditLogger, *, principal: SuperuserPrincipal,
        candidate_id: uuid.UUID, reason: str,
    ) -> DecisionView: ...

    async def reject_candidate(
        self, session: AsyncSession, audit: AuditLogger, *, principal: SuperuserPrincipal,
        candidate_id: uuid.UUID, reason: str,
    ) -> DecisionView: ...

    async def rollback_version(
        self, session: AsyncSession, audit: AuditLogger, *, principal: SuperuserPrincipal,
        version_id: int, reason: str,
    ) -> VersionView: ...
