"""The Judge's control layer (19 §9, 28 §1): human-approved, versioned change.

    improvement candidate (pending) ── superuser approves ──▶ ConfigVersion row
                                    └─ superuser rejects
    ConfigVersion (latest for a target) ── superuser rolls back ──▶ new ConfigVersion row

Three rules, each structural:

* **Only a superuser decides.** Every method takes a `SuperuserPrincipal` —
  produced only by `server.gateway.superuser_auth` — and checks its grant. The
  evaluation package cannot import this module (pyproject: "Only the gateway
  reaches superuser authority", "The Judge is never an authority"), so no
  evaluator can approve a candidate, its own or another's.
* **Only the closed registry.** An approved value is re-validated against
  `server/evaluation/candidates.py` at approval time, not trusted from the
  queue: a target outside the registry can never become a config version.
* **Versioned and reversible.** `config_versions` is append-only; approval and
  rollback each add a row; a target's effective value is its latest row.

The effective values reach the runtime as `WorkerTuning` (worker system-prompt
guidance, tool descriptions shown to the worker, recovery thresholds) and the
Judge as its rubric — guidance and bounds only, never authority. A
`suggestion.template` is versioned like the rest but has no consumer in this
build (no suggestion-template feature exists yet).

The switchboard is the operator's runtime on/off for the Judge and for its
stop requests, each **capped by configuration**: it can turn either off, and
back on only where `evaluation.enabled` / `evaluation.may_request_stop` allow.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.agent.ports import WorkerTuning
from server.config.schema import EvaluationConfig
from server.evaluation.candidates import CandidateRejected, validate_value
from server.gateway.evaluation_control_port import (
    DecisionView,
    EvaluationControlRefusalKind,
    EvaluationControlRefused,
    SwitchesView,
    VersionView,
)
from server.gateway.superuser_auth import SuperuserPrincipal
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.storage import StorageBackend
from server.storage.models import ConfigVersion, ImprovementCandidateRow
from server.storage.models import EvaluationControl as EvaluationControlRow
from shared.schemas.enums import AuditActor, AuditResult
from shared.schemas.evaluation import IDENTIFIER_PATTERN, CandidateStatus

logger = logging.getLogger("hypermind.composition.improvements")

_REASON = re.compile(IDENTIFIER_PATTERN)
CONTROL_ID = 1
TUNING_TTL_SECONDS = 5.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── effective values ───────────────────────────────────────────────────────


async def effective_values(session: AsyncSession) -> dict[str, str]:
    """Each target's latest `config_versions` row, where it sets a value."""

    latest = (
        select(ConfigVersion.target_key, func.max(ConfigVersion.version_id).label("v"))
        .group_by(ConfigVersion.target_key)
        .subquery()
    )
    rows = (await session.execute(
        select(ConfigVersion.target_key, ConfigVersion.value)
        .join(latest, ConfigVersion.version_id == latest.c.v)
    )).all()
    return {key: value for key, value in rows if value is not None}


def worker_tuning(values: dict[str, str]) -> WorkerTuning:
    def number(key: str) -> int | None:
        raw = values.get(key)
        return int(raw) if raw is not None and raw.isdigit() else None

    prefix = "worker.tool_description:"
    return WorkerTuning(
        system_prompt=values.get("worker.system_prompt"),
        tool_descriptions={k[len(prefix):]: v for k, v in values.items() if k.startswith(prefix)},
        stall_window=number("recovery.stall_window"),
        loop_repeat_limit=number("recovery.loop_repeat_limit"),
        max_worker_switches=number("recovery.max_worker_switches"),
    )


class TuningCache:
    """This process's view of the effective values: read with its own session
    (never inside a task's transaction), refreshed after an approval or a
    rollback and at most every few seconds. Unreadable → the base config."""

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage
        self._values: dict[str, str] | None = None
        self._loaded_at = 0.0

    def invalidate(self) -> None:
        self._values = None

    async def values(self) -> dict[str, str]:
        if self._values is None or time.monotonic() - self._loaded_at > TUNING_TTL_SECONDS:
            try:
                async with self._storage.session() as session:
                    self._values = await effective_values(session)
            except Exception:  # noqa: BLE001 — tuning is guidance; its absence is the base config
                logger.warning("approved configuration versions could not be read; using the base configuration")
                return {}
            self._loaded_at = time.monotonic()
        return dict(self._values)

    async def current(self) -> WorkerTuning:  # TuningPort
        return worker_tuning(await self.values())

    async def rubric(self) -> str | None:
        return (await self.values()).get("evaluation.rubric")


# ── the operator's switches ────────────────────────────────────────────────


@dataclass
class EvaluationSwitchboard:
    """Implements `server.evaluation.ports.EvaluationSwitches`. The in-memory
    values are what the Judge reads synchronously; the `evaluation_control`
    row makes an operator's "off" survive a restart."""

    config: EvaluationConfig
    judge: bool | None = None
    stop: bool | None = None
    loaded: bool = False
    _load_failed: bool = field(default=False, repr=False)

    def judge_enabled(self) -> bool:
        if not self.config.enabled or self._load_failed:
            return False
        return self.judge is not False

    def stop_requests_enabled(self) -> bool:
        if not (self.config.enabled and self.config.may_request_stop) or self._load_failed:
            return False
        return self.stop is not False and self.judge_enabled()

    async def ensure_loaded(self, storage: StorageBackend) -> None:
        if self.loaded:
            return
        try:
            async with storage.session() as session:
                row = await session.get(EvaluationControlRow, CONTROL_ID)
        except Exception:  # noqa: BLE001 — unreadable ⇒ Judge off (the safe direction)
            logger.exception("evaluation_control could not be read; the Judge stays off")
            self._load_failed = True
            return
        self._load_failed = False
        if row is not None:
            self.judge, self.stop = row.judge_enabled, row.stop_requests_enabled
        self.loaded = True

    def view(self) -> SwitchesView:
        return SwitchesView(
            configured_enabled=self.config.enabled,
            configured_may_request_stop=self.config.may_request_stop,
            judge_enabled=self.judge_enabled(),
            stop_requests_enabled=self.stop_requests_enabled(),
        )


# ── the superuser control path ─────────────────────────────────────────────


def _require(principal: SuperuserPrincipal) -> None:
    if not isinstance(principal, SuperuserPrincipal) or not principal.grant.is_valid():
        raise PermissionError("superuser authority required")


def _refused(kind: EvaluationControlRefusalKind, code: str, message: str) -> EvaluationControlRefused:
    return EvaluationControlRefused(kind, code, message)


class EvaluationControl:
    """Implements `server.gateway.evaluation_control_port.EvaluationControlPort`."""

    def __init__(self, *, switchboard: EvaluationSwitchboard, tuning: TuningCache,
                 storage: StorageBackend) -> None:
        self._switches = switchboard
        self._tuning = tuning
        self._storage = storage

    async def set_switches(
        self, session: AsyncSession, audit: AuditLogger, *, principal: SuperuserPrincipal,
        judge_enabled: bool | None, stop_requests_enabled: bool | None, reason: str,
    ) -> SwitchesView:
        _require(principal)
        self._check_reason(reason)
        config = self._switches.config
        resource = f"control:evaluation:switches:{reason}"
        # Capped by configuration: "on" is refused where the config says off,
        # so nothing — no operator call, no Judge output — turns on a stop
        # ability the operator's configuration withholds.
        if judge_enabled and not config.enabled:
            await self._audit(audit, resource + ":not_configured", AuditResult.BLOCKED)
            raise _refused(EvaluationControlRefusalKind.CONFLICT, "not_enabled_in_config",
                           "evaluation.enabled is false in the server configuration")
        if stop_requests_enabled and not (config.enabled and config.may_request_stop):
            await self._audit(audit, resource + ":not_configured", AuditResult.BLOCKED)
            raise _refused(EvaluationControlRefusalKind.CONFLICT, "not_enabled_in_config",
                           "evaluation.may_request_stop is false in the server configuration")

        await self._switches.ensure_loaded(self._storage)
        judge = self._switches.judge if judge_enabled is None else judge_enabled
        stop = self._switches.stop if stop_requests_enabled is None else stop_requests_enabled
        row = await session.get(EvaluationControlRow, CONTROL_ID)
        if row is None:
            row = EvaluationControlRow(control_id=CONTROL_ID, judge_enabled=judge is not False,
                                    stop_requests_enabled=stop is not False, changed_at=_utcnow(),
                                    changed_by=principal.token_fingerprint)
            session.add(row)
        else:
            row.judge_enabled, row.stop_requests_enabled = judge is not False, stop is not False
            row.changed_at, row.changed_by = _utcnow(), principal.token_fingerprint
        await session.flush()
        # Memory after the row: turning something *off* takes effect for the
        # next evaluation this process starts.
        self._switches.judge, self._switches.stop = row.judge_enabled, row.stop_requests_enabled
        self._switches.loaded = True
        view = self._switches.view()
        state = f"judge_{'on' if view.judge_enabled else 'off'}:stop_{'on' if view.stop_requests_enabled else 'off'}"
        await self._audit(audit, f"{resource}:{state}", AuditResult.SUCCESS)
        return view

    async def approve_candidate(
        self, session: AsyncSession, audit: AuditLogger, *, principal: SuperuserPrincipal,
        candidate_id: uuid.UUID, reason: str,
    ) -> DecisionView:
        _require(principal)
        self._check_reason(reason)
        row = await self._pending(session, audit, candidate_id, "approve", reason)
        try:
            value = validate_value(row.target, row.subject, row.proposed_value)
        except CandidateRejected as exc:
            await self._audit(audit, f"control:evaluation:approve:{candidate_id}:{exc.code}", AuditResult.BLOCKED)
            raise _refused(EvaluationControlRefusalKind.VALIDATION, exc.code,
                           "the candidate's target or value is not allowed") from None
        target_key = f"{row.target}:{row.subject}" if row.subject else row.target
        version = ConfigVersion(target_key=target_key, value=str(value), action="approve",
                                candidate_id=row.candidate_id, reason=reason, created_at=_utcnow(),
                                created_by=principal.token_fingerprint)
        session.add(version)
        await session.flush()
        row.status = CandidateStatus.APPROVED.value
        row.decided_at, row.decided_by, row.decision_reason = _utcnow(), principal.token_fingerprint, reason
        row.config_version_id = version.version_id
        await session.flush()
        await audit.record(actor=AuditActor.SUPERUSER, action=AuditAction.IMPROVEMENT_CANDIDATE_APPROVED,
                           resource=f"candidate:{candidate_id}:{reason}", result=AuditResult.SUCCESS,
                           user_id=row.source_user_id)
        await audit.record(actor=AuditActor.SUPERUSER, action=AuditAction.CONFIG_VERSION_APPLIED,
                           resource=f"config_version:{version.version_id}:{target_key}"[:128],
                           result=AuditResult.SUCCESS)
        await self._audit(audit, f"control:evaluation:approve:{candidate_id}:{reason}", AuditResult.SUCCESS)
        self._tuning.invalidate()
        return DecisionView(candidate_id=candidate_id, status=row.status, config_version_id=version.version_id,
                            target_key=target_key)

    async def reject_candidate(
        self, session: AsyncSession, audit: AuditLogger, *, principal: SuperuserPrincipal,
        candidate_id: uuid.UUID, reason: str,
    ) -> DecisionView:
        _require(principal)
        self._check_reason(reason)
        row = await self._pending(session, audit, candidate_id, "reject", reason)
        row.status = CandidateStatus.REJECTED.value
        row.decided_at, row.decided_by, row.decision_reason = _utcnow(), principal.token_fingerprint, reason
        await session.flush()
        await audit.record(actor=AuditActor.SUPERUSER, action=AuditAction.IMPROVEMENT_CANDIDATE_REJECTED,
                           resource=f"candidate:{candidate_id}:{reason}", result=AuditResult.SUCCESS,
                           user_id=row.source_user_id)
        await self._audit(audit, f"control:evaluation:reject:{candidate_id}:{reason}", AuditResult.SUCCESS)
        target_key = f"{row.target}:{row.subject}" if row.subject else row.target
        return DecisionView(candidate_id=candidate_id, status=row.status, config_version_id=None,
                            target_key=target_key)

    async def rollback_version(
        self, session: AsyncSession, audit: AuditLogger, *, principal: SuperuserPrincipal,
        version_id: int, reason: str,
    ) -> VersionView:
        """Undo the target's **current** version: a new row restores the value
        before it (or the base configuration). Only the latest version of a
        target can be rolled back — anything older is already superseded."""

        _require(principal)
        self._check_reason(reason)
        resource = f"control:evaluation:rollback:{version_id}:{reason}"
        version = await session.get(ConfigVersion, version_id)
        if version is None:
            await self._audit(audit, resource + ":not_found", AuditResult.FAILURE)
            raise _refused(EvaluationControlRefusalKind.NOT_FOUND, "not_found", "no such configuration version")
        latest = (await session.execute(
            select(func.max(ConfigVersion.version_id)).where(ConfigVersion.target_key == version.target_key)
        )).scalar_one()
        if latest != version.version_id:
            await self._audit(audit, resource + ":not_current", AuditResult.BLOCKED)
            raise _refused(EvaluationControlRefusalKind.CONFLICT, "not_current",
                           "only a target's current version can be rolled back")
        previous = (await session.execute(
            select(ConfigVersion.value)
            .where(ConfigVersion.target_key == version.target_key, ConfigVersion.version_id < version.version_id)
            .order_by(ConfigVersion.version_id.desc()).limit(1)
        )).scalar_one_or_none()
        row = ConfigVersion(target_key=version.target_key, value=previous, action="rollback",
                            rolled_back_version_id=version.version_id, reason=reason, created_at=_utcnow(),
                            created_by=principal.token_fingerprint)
        session.add(row)
        await session.flush()
        await audit.record(actor=AuditActor.SUPERUSER, action=AuditAction.CONFIG_VERSION_ROLLED_BACK,
                           resource=f"config_version:{row.version_id}:rolls_back:{version.version_id}",
                           result=AuditResult.SUCCESS)
        await self._audit(audit, resource, AuditResult.SUCCESS)
        self._tuning.invalidate()
        return VersionView(version_id=row.version_id, target_key=row.target_key, action=row.action,
                           rolled_back_version_id=version.version_id)

    # ── shared ──────────────────────────────────────────────────────────

    async def _pending(self, session: AsyncSession, audit: AuditLogger, candidate_id: uuid.UUID,
                       verb: str, reason: str) -> ImprovementCandidateRow:
        row = await session.get(ImprovementCandidateRow, candidate_id)
        if row is None:
            await self._audit(audit, f"control:evaluation:{verb}:{candidate_id}:not_found", AuditResult.FAILURE)
            raise _refused(EvaluationControlRefusalKind.NOT_FOUND, "not_found", "no such candidate")
        if row.status != CandidateStatus.PENDING.value:
            await self._audit(audit, f"control:evaluation:{verb}:{candidate_id}:not_pending", AuditResult.BLOCKED)
            raise _refused(EvaluationControlRefusalKind.CONFLICT, "not_pending", "the candidate was already decided")
        return row

    @staticmethod
    def _check_reason(reason: str) -> None:
        if not _REASON.fullmatch(reason or ""):
            raise ValueError("reason must be a short identifier")

    @staticmethod
    async def _audit(audit: AuditLogger, resource: str, result: AuditResult) -> None:
        await audit.record(actor=AuditActor.SUPERUSER, action=AuditAction.CONTROL_EVALUATION,
                           resource=resource[:128], result=result)


__all__ = [
    "EvaluationControl",
    "EvaluationSwitchboard",
    "TuningCache",
    "effective_values",
    "worker_tuning",
]
