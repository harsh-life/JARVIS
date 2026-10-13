"""The caller's own primary model and model keys (pilot plan C7, step M2;
OD-PILOT-2 as confirmed 2026-10-13, interpretation 1).

* `GET  /api/v1/config/model/catalog` — what the operator published: the
  configured primary (`agent.primary`) and fallback (`agent.fallback`), each
  with its provider, model and prices. Never an endpoint or a key reference.
* `GET  /api/v1/config/model` — the caller's selection, or the server default.
* `PUT  /api/v1/config/model` — select a catalog entry, with the caller's own
  key for a paid one. Writes the caller's `AgentConfiguration(scope = user)`
  row and nothing else: no graph id is accepted (graph-scoped model
  configuration is deferred), no endpoint (the resolver takes it from the
  operator's configuration — a user never chooses where the server connects),
  no `env:` reference (the server's own environment is not a user's to name).
* `DELETE /api/v1/config/model` — back to the server default.
* `POST /api/v1/secrets` — store one model API key; the answer is **only** its
  handle. The value is read from the raw body by hand: it never passes
  through request validation, whose error envelope echoes inputs, and it is
  never logged, audited or returned.
* `DELETE /api/v1/secrets/{key_ref}` — revoke one of the caller's keys.

A key is stored user-scoped (`SecretOwnerScopeType.USER`) and class
`model_api_key`; only the caller's own requests resolve it (12 §2). Another
user's handle, a revoked one, or any other class is "not found".
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config.schema import LOCAL_MODEL_PROVIDERS, AppConfig, ModelEntryConfig
from server.gateway.deps import get_audit_logger, get_db_session, get_principal, get_security_core
from server.gateway.errors import AppError
from server.gateway.security import SecurityCore
from server.secrets.requester import SecretRequester
from server.secrets.store import HANDLE_PREFIX
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.storage.models import AgentConfiguration, SecretReference
from shared.schemas.agent_config import ModelConfiguration
from shared.schemas.authorization import Principal, device_of, session_of
from shared.schemas.enums import AgentConfigScopeType, AuditActor, AuditResult, SecretClass, SecretOwnerScopeType
from shared.schemas.errors import ErrorCode

router = APIRouter(tags=["model-config"])

CatalogId = Literal["agent.primary", "agent.fallback"]
MAX_KEY_CHARS = 4096
MAX_KEY_BODY_BYTES = 8192
MAX_KEYS_PER_USER = 10


class Pricing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_per_1k_tokens: float
    output_per_1k_tokens: float


class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: CatalogId
    provider: str
    model: str
    local: bool
    pricing: Pricing | None
    # A paid entry is selectable only with the caller's own key; the
    # operator's key serves only the server default (no row at all).
    requires_user_key: bool
    server_default: bool


class CatalogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CatalogEntry]


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["server_default", "user"]
    catalog_id: CatalogId | None
    provider: str
    model: str
    key_ref: str | None
    # False when the selected key has been revoked: the model then fails
    # closed (`model_unavailable`) until the user selects again.
    key_usable: bool | None


class ModelSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    catalog_id: CatalogId
    key_ref: str | None = Field(default=None, max_length=128)


def _row_ref(key_ref: str) -> str:
    """A stored row names a key in the configuration vocabulary (15 §2):
    `secretstore:` + the store's handle — which itself begins `secretstore:`.
    The models boundary (`server/composition/secret_context.py`) strips exactly
    one prefix before resolving, so this is the form that resolves."""

    return f"{HANDLE_PREFIX}{key_ref}"


def _key_of_row(secret_ref: str | None) -> str | None:
    if secret_ref is None:
        return None
    return secret_ref[len(HANDLE_PREFIX):] if secret_ref.startswith(HANDLE_PREFIX + HANDLE_PREFIX) else secret_ref


class StoredKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key_ref: str


def catalog_from_config(config: AppConfig | None) -> dict[str, ModelEntryConfig]:
    if config is None:
        return {}
    entries: dict[str, ModelEntryConfig] = {"agent.primary": config.agent}
    if config.agent.fallback is not None:
        entries["agent.fallback"] = config.agent.fallback
    return entries


def _catalog(request: Request) -> dict[str, ModelEntryConfig]:
    return getattr(request.app.state, "model_catalog", None) or {}


def _entry_view(entry_id: str, entry: ModelEntryConfig) -> CatalogEntry:
    local = entry.provider in LOCAL_MODEL_PROVIDERS
    return CatalogEntry(
        id=entry_id,  # type: ignore[arg-type]
        provider=entry.provider,
        model=entry.model,
        local=local,
        pricing=Pricing(**entry.pricing.model_dump()) if entry.pricing is not None else None,
        requires_user_key=not local,
        server_default=entry_id == "agent.primary",
    )


async def _row(session: AsyncSession, principal: Principal) -> AgentConfiguration | None:
    return (await session.execute(
        select(AgentConfiguration).where(
            AgentConfiguration.scope_type == AgentConfigScopeType.USER,
            AgentConfiguration.scope_id == principal.user_id,
        )
    )).scalars().first()


async def _own_key(session: AsyncSession, principal: Principal, key_ref: str) -> SecretReference | None:
    """The caller's live model key with this handle, or None — another user's,
    a revoked one and any other class all look the same."""

    if not key_ref.startswith(HANDLE_PREFIX):
        return None
    reference = await session.get(SecretReference, key_ref)
    if (
        reference is None
        or reference.revoked_at is not None
        or reference.class_ is not SecretClass.MODEL_API_KEY
        or reference.owner_scope_type is not SecretOwnerScopeType.USER
        or reference.owner_scope_id != str(principal.user_id)
    ):
        return None
    return reference


async def _audit(audit: AuditLogger, principal: Principal, action: AuditAction, resource: str) -> None:
    await audit.record(
        actor=AuditActor.USER, action=action, resource=resource, result=AuditResult.SUCCESS,
        user_id=principal.user_id, device_id=device_of(principal), session_id=session_of(principal),
    )


@router.get("/config/model/catalog", response_model=CatalogResponse)
async def model_catalog(request: Request, principal: Principal = Depends(get_principal)) -> CatalogResponse:
    return CatalogResponse(items=[_entry_view(k, v) for k, v in _catalog(request).items()])


@router.get("/config/model", response_model=ModelSelection)
async def get_model(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
) -> ModelSelection:
    catalog = _catalog(request)
    row = await _row(session, principal)
    if row is None:
        default = catalog.get("agent.primary")
        if default is None:
            raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "no model is configured on this server",
                           details={"dependency": "model"})
        return ModelSelection(source="server_default", catalog_id="agent.primary", provider=default.provider,
                              model=default.model, key_ref=None, key_usable=None)
    try:
        chosen = ModelConfiguration.model_validate(row.primary_model)
    except Exception:  # noqa: BLE001 — a malformed row is reported as unusable, never echoed
        raise AppError(ErrorCode.CONFLICT, "the stored model configuration is invalid; select a model again") from None
    catalog_id = next(
        (k for k, v in catalog.items() if v.provider == chosen.provider.value and v.model == chosen.model), None
    )
    key_ref = _key_of_row(chosen.secret_ref)
    key_usable = None
    if key_ref:
        key_usable = await _own_key(session, principal, key_ref) is not None
    return ModelSelection(source="user", catalog_id=catalog_id, provider=chosen.provider.value,
                          model=chosen.model, key_ref=key_ref, key_usable=key_usable)


@router.put("/config/model", response_model=ModelSelection)
async def put_model(
    request: Request,
    body: ModelSelectionRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> ModelSelection:
    entry = _catalog(request).get(body.catalog_id)
    if entry is None:
        raise AppError(ErrorCode.VALIDATION_FAILED, "that model is not offered by this server")
    local = entry.provider in LOCAL_MODEL_PROVIDERS
    if local and body.key_ref is not None:
        raise AppError(ErrorCode.VALIDATION_FAILED, "a local model takes no key")
    if not local and body.key_ref is None:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            "this model needs your own key; to use the server's, return to the server default",
        )
    if body.key_ref is not None and await _own_key(session, principal, body.key_ref) is None:
        raise AppError(ErrorCode.NOT_FOUND, "not found")

    try:
        document = ModelConfiguration.model_validate({
            "provider": entry.provider,
            "model": entry.model,
            "secret_ref": _row_ref(body.key_ref) if body.key_ref else None,
            "timeout_seconds": max(1, int(entry.timeout_seconds)),
        }).model_dump(mode="json", exclude_none=True)
    except ValueError:
        raise AppError(ErrorCode.CONFLICT, "that model cannot be selected per user on this server") from None
    now = datetime.now(timezone.utc)
    row = await _row(session, principal)
    if row is None:
        # Only the model is configured here: `enabled_tools`/`model_tools`
        # stay unset, so the user's tools are the server's (see
        # ConfiguredModelResolver).
        session.add(AgentConfiguration(scope_type=AgentConfigScopeType.USER, scope_id=principal.user_id,
                                       primary_model=document, updated_at=now))
    else:
        row.primary_model = document
        row.updated_at = now
    await session.flush()
    await _audit(audit, principal, AuditAction.MODEL_CONFIG_CHANGED, f"config:model:user:{principal.user_id}")
    return ModelSelection(source="user", catalog_id=body.catalog_id, provider=entry.provider, model=entry.model,
                          key_ref=body.key_ref, key_usable=True if body.key_ref else None)


@router.delete("/config/model", status_code=status.HTTP_204_NO_CONTENT)
async def reset_model(
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> Response:
    row = await _row(session, principal)
    if row is not None:
        if row.enabled_tools is not None or row.model_tools is not None or row.granted_capabilities is not None:
            # A row that also restricts tools was not written by this API;
            # deleting it would widen what the user's tasks may use.
            raise AppError(ErrorCode.CONFLICT, "this configuration is managed by the operator")
        await session.delete(row)
        await session.flush()
        await _audit(audit, principal, AuditAction.MODEL_CONFIG_CHANGED, f"config:model:user:{principal.user_id}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _refuse_key(message: str) -> AppError:
    # Never the value, never its length, never the parse error's text.
    return AppError(ErrorCode.VALIDATION_FAILED, message)


@router.post("/secrets", response_model=StoredKey, status_code=status.HTTP_201_CREATED)
async def store_key(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    core: SecurityCore = Depends(get_security_core),
    audit: AuditLogger = Depends(get_audit_logger),
) -> StoredKey:
    raw = await request.body()
    if len(raw) > MAX_KEY_BODY_BYTES:
        raise _refuse_key("the request is too large")
    try:
        body = json.loads(raw)
    except ValueError:
        raise _refuse_key("the request must be a JSON object with one field, value") from None
    if not isinstance(body, dict) or set(body) != {"value"} or not isinstance(body["value"], str):
        raise _refuse_key("the request must be a JSON object with one field, value")
    value = body["value"].strip()
    if not value or len(value) > MAX_KEY_CHARS or any(c.isspace() for c in value):
        raise _refuse_key("that is not a usable API key")
    held = (await session.execute(
        select(func.count()).select_from(SecretReference).where(
            SecretReference.owner_scope_type == SecretOwnerScopeType.USER,
            SecretReference.owner_scope_id == str(principal.user_id),
            SecretReference.class_ == SecretClass.MODEL_API_KEY,
            SecretReference.revoked_at.is_(None),
        )
    )).scalar_one()
    if held >= MAX_KEYS_PER_USER:
        raise AppError(ErrorCode.CONFLICT, "too many stored keys; delete one first")
    handle = await core.secret_store.set(
        session,
        owner_scope_type=SecretOwnerScopeType.USER,
        owner_scope_id=str(principal.user_id),
        secret_class=SecretClass.MODEL_API_KEY,
        value=value,
        requester=SecretRequester.user(principal.user_id),
        audit=audit,
    )
    return StoredKey(key_ref=handle)


@router.delete("/secrets/{key_ref}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_key(
    key_ref: str,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    core: SecurityCore = Depends(get_security_core),
    audit: AuditLogger = Depends(get_audit_logger),
) -> Response:
    if await _own_key(session, principal, key_ref) is None:
        raise AppError(ErrorCode.NOT_FOUND, "not found")
    await core.secret_store.delete(session, key_ref, SecretRequester.user(principal.user_id), audit)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["catalog_from_config", "router"]
