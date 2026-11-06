"""docs/30 — the Intelligence Workbench's configuration API (Phase 0).

Configuration only. Nothing here runs a model, reaches a provider, resolves a
secret or changes what a task may do; the production request lifecycle reads
none of it (`production_active` is always false). The user's *production*
model stays M2's `/config/model` — this API never writes it.

* `GET    /intelligence/catalog` — what the operator offers: models (no
  endpoint, no key), orchestration backends with an honest integration status
  decided by code, specializations and their prompt profiles, the ceilings.
* `POST   /intelligence/validate` — check a document without saving it.
* `GET    /intelligence/profiles` — the caller's own profiles.
* `POST   /intelligence/profiles` — create one (version 1).
* `GET    /intelligence/profiles/{id}` — a profile, its versions, the latest.
* `POST   /intelligence/profiles/{id}/versions` — save a new immutable version;
  `base_version` must be the current one (no silent overwrite of a concurrent
  edit).
* `GET    /intelligence/profiles/{id}/versions/{v}` — one version.
* `GET    /intelligence/profiles/{id}/versions/{v}/snapshot` — the immutable
  `ExperimentSnapshot` the harness runs, re-validated against today's catalogue.
* `DELETE /intelligence/profiles/{id}` — delete a profile and its versions.

Identity is the authenticated principal only: no request body carries an
owner, and another user's profile is `not_found`, indistinguishable from none.
With `workbench.enabled: false` every route authenticates and then answers
`503 dependency_unavailable` (`workbench`).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.config.schema import AppConfig
from server.config.workbench import (
    Issue,
    backend_state,
    build_snapshot,
    catalog_models,
    integration_of,
    model_state,
    offered_backends,
    offered_specializations,
    profile_status,
    validate_profile,
)
from server.gateway.deps import get_audit_logger, get_db_session, get_principal
from server.gateway.errors import AppError
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.storage.models import IntelligenceProfileRow, IntelligenceProfileVersionRow
from shared.schemas.authorization import Principal, device_of, session_of
from shared.schemas.enums import AuditActor, AuditResult
from shared.schemas.errors import ErrorCode
from shared.schemas.intelligence import (
    CENTRAL_PROMPT_PROFILES,
    ExperimentSnapshot,
    IntelligenceProfileDocument,
    profile_hash,
)

router = APIRouter(tags=["intelligence"])


def _config(request: Request) -> AppConfig:
    config: AppConfig | None = getattr(request.app.state, "workbench_config", None)
    if config is None or not config.workbench.enabled:
        raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "the intelligence workbench is not enabled on this server",
                       details={"dependency": "workbench"})
    return config


class ProfileName(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def _printable(cls, value: str) -> str:
        value = value.strip()
        if not value or any(not c.isprintable() for c in value):
            raise ValueError("a name is one line of printable text")
        return value


class CreateProfileRequest(ProfileName):
    document: dict[str, Any]


class NewVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_version: int = Field(ge=1)
    document: dict[str, Any]


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document: dict[str, Any]


def _parse(document: dict[str, Any]) -> IntelligenceProfileDocument:
    """Parse a document, refusing it whole with field paths — never echoing
    the submitted values (the profile holds no secrets, but the envelope
    stays as terse as everywhere else)."""

    try:
        return IntelligenceProfileDocument.model_validate(document)
    except ValidationError as exc:
        issues = [
            Issue(".".join(str(p) for p in err["loc"]) or "document", "invalid", err["msg"]).as_dict()
            for err in exc.errors(include_input=False, include_url=False)
        ]
        raise AppError(ErrorCode.VALIDATION_FAILED, "the profile document is not valid",
                       details={"issues": issues[:50]}) from None


def _checked(document: IntelligenceProfileDocument, config: AppConfig) -> list[Issue]:
    issues = validate_profile(document, config)
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        raise AppError(ErrorCode.VALIDATION_FAILED, "the profile does not fit this server's catalogue",
                       details={"issues": [i.as_dict() for i in issues]})
    return issues


def _version_view(row: IntelligenceProfileVersionRow, config: AppConfig) -> dict[str, Any]:
    document = IntelligenceProfileDocument.model_validate(row.document)
    # Validated against *today's* catalogue: a saved version never changes,
    # but the operator's catalogue can, and the Workbench says so.
    issues = validate_profile(document, config)
    return {
        "version": row.version,
        "config_hash": row.config_hash,
        "created_at": row.created_at.isoformat(),
        "document": document.model_dump(mode="json"),
        "validation": {"valid": not any(i.severity == "error" for i in issues),
                       "issues": [i.as_dict() for i in issues]},
        "status": profile_status(document, config, issues),
    }


def _profile_summary(row: IntelligenceProfileRow) -> dict[str, Any]:
    return {"profile_id": str(row.profile_id), "name": row.name, "current_version": row.current_version,
            "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat()}


async def _own_profile(session: AsyncSession, principal: Principal, profile_id: str) -> IntelligenceProfileRow:
    try:
        pid = uuid.UUID(profile_id)
    except ValueError:
        raise AppError(ErrorCode.NOT_FOUND, "not found") from None
    row = await session.get(IntelligenceProfileRow, pid)
    if row is None or row.owner_user_id != principal.user_id:
        raise AppError(ErrorCode.NOT_FOUND, "not found")
    return row


async def _version(session: AsyncSession, profile: IntelligenceProfileRow, version: int) -> IntelligenceProfileVersionRow:
    row = await session.get(IntelligenceProfileVersionRow, (profile.profile_id, version))
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "not found")
    return row


async def _audit(audit: AuditLogger, principal: Principal, action: AuditAction, resource: str) -> None:
    await audit.record(
        actor=AuditActor.USER, action=action, resource=resource, result=AuditResult.SUCCESS,
        user_id=principal.user_id, device_id=device_of(principal), session_id=session_of(principal),
    )


@router.get("/intelligence/catalog")
async def catalog(request: Request, principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    config = _config(request)
    workbench = config.workbench
    models = []
    for model in catalog_models(config).values():
        pricing = model.entry.pricing
        models.append({
            "catalog_id": model.catalog_id,
            "display_name": model.display_name,
            "provider": model.entry.provider,
            "model": model.entry.model,
            "local": model.local,
            "pricing": ({"input_per_1k_tokens": pricing.input_per_1k_tokens,
                         "output_per_1k_tokens": pricing.output_per_1k_tokens} if pricing else None),
            "production_selectable": model.production_selectable,
            "experiment_state": model_state(model).value,
        })
    backends = []
    for backend in offered_backends(workbench).values():
        backends.append({
            "backend_id": backend.id,
            "display_name": backend.display_name,
            "kind": backend.kind.value,
            "adapter": backend.adapter,
            "integration": integration_of(backend).value,
            "state": backend_state(backend).value,
            "production_active": False,
            "runtime": backend.runtime,
            "requires_model": backend.requires_model,
            "allowed_models": backend.allowed_models,
            # Whether an operator-held endpoint is configured — never the value.
            "endpoint_configured": backend.endpoint_ref is not None,
            "delegation_mode": backend.delegation_mode.value,
            "state_model": backend.state_model.value,
            "supports_parallel": backend.supports_parallel,
            "capabilities": backend.capabilities,
            "limitations": backend.limitations,
            "max_timeout_seconds": backend.max_timeout_seconds,
            "max_fan_out": backend.max_fan_out,
            "max_budget": backend.max_budget,
            "config_version": backend.config_version,
        })
    specializations = [
        {"id": s.id, "display_name": s.display_name, "description": s.description, "output_schema": s.output_schema,
         "prompt_profiles": [{"id": p.id, "versions": p.versions} for p in s.prompt_profiles]}
        for s in offered_specializations(workbench).values()
    ]
    return {
        "production_active": False,
        "models": models,
        "orchestration_backends": backends,
        "specializations": specializations,
        "central_prompt_profiles": [{"id": k, "versions": sorted(v)} for k, v in CENTRAL_PROMPT_PROFILES.items()],
        "limits": workbench.limits.model_dump(),
    }


@router.post("/intelligence/validate")
async def validate(request: Request, body: ValidateRequest,
                   principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    config = _config(request)
    document = _parse(body.document)
    issues = validate_profile(document, config)
    return {
        "valid": not any(i.severity == "error" for i in issues),
        "issues": [i.as_dict() for i in issues],
        "config_hash": profile_hash(document),
        "document": document.model_dump(mode="json"),
        "status": profile_status(document, config, issues),
    }


@router.get("/intelligence/profiles")
async def list_profiles(request: Request, session: AsyncSession = Depends(get_db_session),
                        principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    _config(request)
    rows = (await session.execute(
        select(IntelligenceProfileRow).where(IntelligenceProfileRow.owner_user_id == principal.user_id)
        .order_by(IntelligenceProfileRow.updated_at.desc())
    )).scalars().all()
    return {"items": [_profile_summary(r) for r in rows]}


@router.post("/intelligence/profiles", status_code=status.HTTP_201_CREATED)
async def create_profile(request: Request, body: CreateProfileRequest,
                         session: AsyncSession = Depends(get_db_session),
                         principal: Principal = Depends(get_principal),
                         audit: AuditLogger = Depends(get_audit_logger)) -> dict[str, Any]:
    config = _config(request)
    document = _parse(body.document)
    _checked(document, config)
    held = (await session.execute(
        select(func.count()).select_from(IntelligenceProfileRow)
        .where(IntelligenceProfileRow.owner_user_id == principal.user_id)
    )).scalar_one()
    if held >= config.workbench.limits.max_profiles_per_user:
        raise AppError(ErrorCode.CONFLICT, "too many profiles; delete one first")
    now = datetime.now(timezone.utc)
    profile = IntelligenceProfileRow(owner_user_id=principal.user_id, name=body.name, current_version=1,
                                     created_at=now, updated_at=now)
    session.add(profile)
    await session.flush()
    version = IntelligenceProfileVersionRow(profile_id=profile.profile_id, version=1,
                                            config_hash=profile_hash(document),
                                            document=document.model_dump(mode="json"), created_at=now)
    session.add(version)
    await session.flush()
    await _audit(audit, principal, AuditAction.INTELLIGENCE_PROFILE_CREATED,
                 f"config:intelligence_profile:{profile.profile_id}:v1")
    return {**_profile_summary(profile), "latest": _version_view(version, config)}


@router.get("/intelligence/profiles/{profile_id}")
async def get_profile(profile_id: str, request: Request, session: AsyncSession = Depends(get_db_session),
                      principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    config = _config(request)
    profile = await _own_profile(session, principal, profile_id)
    versions = (await session.execute(
        select(IntelligenceProfileVersionRow).where(IntelligenceProfileVersionRow.profile_id == profile.profile_id)
        .order_by(IntelligenceProfileVersionRow.version.desc())
    )).scalars().all()
    latest = next(v for v in versions if v.version == profile.current_version)
    return {
        **_profile_summary(profile),
        "versions": [{"version": v.version, "config_hash": v.config_hash, "created_at": v.created_at.isoformat()}
                     for v in versions],
        "latest": _version_view(latest, config),
    }


@router.post("/intelligence/profiles/{profile_id}/versions", status_code=status.HTTP_201_CREATED)
async def new_version(profile_id: str, request: Request, body: NewVersionRequest,
                      session: AsyncSession = Depends(get_db_session),
                      principal: Principal = Depends(get_principal),
                      audit: AuditLogger = Depends(get_audit_logger)) -> dict[str, Any]:
    config = _config(request)
    profile = await _own_profile(session, principal, profile_id)
    if body.base_version != profile.current_version:
        raise AppError(ErrorCode.CONFLICT, "the profile changed since you loaded it; reload and save again",
                       details={"current_version": profile.current_version})
    if profile.current_version >= config.workbench.limits.max_versions_per_profile:
        raise AppError(ErrorCode.CONFLICT, "this profile has reached its version limit; create a new profile")
    document = _parse(body.document)
    _checked(document, config)
    now = datetime.now(timezone.utc)
    number = profile.current_version + 1
    version = IntelligenceProfileVersionRow(profile_id=profile.profile_id, version=number,
                                            config_hash=profile_hash(document),
                                            document=document.model_dump(mode="json"), created_at=now)
    session.add(version)
    profile.current_version = number
    profile.updated_at = now
    try:
        await session.flush()
    except IntegrityError:
        # A concurrent save took this number first (primary key on
        # profile_id + version): refuse rather than overwrite.
        raise AppError(ErrorCode.CONFLICT, "the profile changed since you loaded it; reload and save again") from None
    await _audit(audit, principal, AuditAction.INTELLIGENCE_PROFILE_VERSIONED,
                 f"config:intelligence_profile:{profile.profile_id}:v{number}")
    return {**_profile_summary(profile), "latest": _version_view(version, config)}


@router.get("/intelligence/profiles/{profile_id}/versions/{version}")
async def get_version(profile_id: str, version: int, request: Request,
                      session: AsyncSession = Depends(get_db_session),
                      principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    config = _config(request)
    profile = await _own_profile(session, principal, profile_id)
    return {"profile_id": str(profile.profile_id), "name": profile.name,
            **_version_view(await _version(session, profile, version), config)}


@router.get("/intelligence/profiles/{profile_id}/versions/{version}/snapshot")
async def export_snapshot(profile_id: str, version: int, request: Request,
                          session: AsyncSession = Depends(get_db_session),
                          principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    config = _config(request)
    profile = await _own_profile(session, principal, profile_id)
    row = await _version(session, profile, version)
    document = IntelligenceProfileDocument.model_validate(row.document)
    if profile_hash(document) != row.config_hash:
        raise AppError(ErrorCode.CONFLICT, "the stored version does not match its hash")
    issues = validate_profile(document, config)
    if any(i.severity == "error" for i in issues):
        raise AppError(ErrorCode.CONFLICT, "this version no longer fits the server's catalogue; save a new version",
                       details={"issues": [i.as_dict() for i in issues]})
    snapshot: ExperimentSnapshot = build_snapshot(profile_id=str(profile.profile_id), version=row.version,
                                                  document=document, config=config)
    return snapshot.model_dump(mode="json")


@router.delete("/intelligence/profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(profile_id: str, request: Request, session: AsyncSession = Depends(get_db_session),
                         principal: Principal = Depends(get_principal),
                         audit: AuditLogger = Depends(get_audit_logger)) -> Response:
    _config(request)
    profile = await _own_profile(session, principal, profile_id)
    await session.execute(delete(IntelligenceProfileVersionRow)
                          .where(IntelligenceProfileVersionRow.profile_id == profile.profile_id))
    await session.delete(profile)
    await session.flush()
    await _audit(audit, principal, AuditAction.INTELLIGENCE_PROFILE_DELETED,
                 f"config:intelligence_profile:{profile.profile_id}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
