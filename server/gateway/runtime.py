"""The runtime composition root — wires `server.agent`'s ports to Security
Core (`server.graph`/`server.capabilities`/`server.secrets`) and to the
runtime's own tool/model/memory packages.

This is the second composition root alongside `server/gateway/security.py`,
and for the same reason: `server/agent`, `server/tools`, `server/modeltools`,
`server/models`, and `server/memory` are all mechanically forbidden from
importing each other or Security Core directly (see `server/agent/ports.py`'s
module docstring), so *something* has to sit above all of them and build the
concrete objects that satisfy each Protocol. `server/gateway` is that
something — it already plays this role for auth/graph/capabilities/secrets.

Two lifetimes, mirroring `security.py`'s own split:

* `RuntimeCore` — built once at app startup (`build_runtime_core`), holding
  the `ToolRegistry` and `TaskStore` that must outlive any single request (a
  task created by one request is resumed by a later one; a tool registered
  once must not be rebuilt per request).
* Everything else in this module (`EngineAuthorizer`, `RegistryToolDispatcher`,
  `ModelInvokerAdapter`, `GatewayEventRecorder`) is built **per request**,
  because each closes over that request's `AsyncSession`, `AuditLogger`, and
  authenticated `Principal` — exactly the pattern `server/gateway/deps.py`
  already uses for `AuditLogger` itself.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.agent.orchestrator import AgentOrchestrator
from server.agent.ports import RuntimeAuthorizationResult
from server.agent.tasks import TaskStore
from server.capabilities.confirmation import ConfirmationRefused, ConfirmationService
from server.capabilities.floor import AbsoluteFloorViolation
from server.config.schema import AppConfig
from server.gateway.security import SecurityCore
from server.graph.authorization import AuthorizationEngine
from server.memory.hydrator import NullMemoryHydrator
from server.modeltools.executor import ModelToolExecutor
from server.models.provider import build_provider
from server.secrets.audit_port import SecretAuditSink
from server.secrets.errors import SecretStoreError
from server.secrets.requester import SecretRequester
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.storage.models import AgentConfiguration as AgentConfigurationRow
from server.storage.models import UsageEvent as UsageEventRow
from server.tools.registry import ToolRegistry
from shared.schemas.agent_config import AgentConfiguration, ModelConfiguration, ToolConfiguration, ToolContract
from shared.schemas.authorization import AccessRequest, Principal
from shared.schemas.enums import (
    AgentConfigScopeType,
    AuditActor,
    AuditResult,
    ModelProvider as ModelProviderName,
    RiskCategory,
    UsageKind,
)
from shared.schemas.runtime import (
    AgentEvent,
    GenerationPolicy,
    ModelMessage,
    ModelResult,
    ModelUnavailable,
    RuntimeBounds,
    RuntimeEventKind,
    ToolExecutionFailed,
    ToolInvocationRequest,
    ToolResult,
    UnsupportedModelProvider,
)

logger = logging.getLogger("hypermind.gateway.runtime")

MODEL_TOOL_CAPABILITY = "model_tool.invoke"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── RuntimeCore: built once at startup ───────────────────────────────────────


class RuntimeCore:
    """Everything the runtime needs that must outlive a single request."""

    def __init__(self, *, tool_registry: ToolRegistry, task_store: TaskStore, bounds: RuntimeBounds) -> None:
        self.tool_registry = tool_registry
        self.task_store = task_store
        self.bounds = bounds


def build_runtime_core(config: AppConfig) -> RuntimeCore:
    """Built **empty** of model-tools (mirrors `build_security_core`'s
    SecretStore being built **locked**) — `populate_tool_registry` is the
    separate async startup step that actually resolves secrets and registers
    them, called from the app's lifespan alongside `unlock_secret_store`.
    """

    return RuntimeCore(
        tool_registry=ToolRegistry(),
        task_store=TaskStore(),
        bounds=RuntimeBounds(**config.agent.bounds.model_dump()),
    )


async def populate_tool_registry(
    runtime: RuntimeCore, *, config: AppConfig, security: SecurityCore, session: AsyncSession
) -> None:
    """Registers every enabled `models_as_tools` entry (`01` §9.3) as a
    model-tool (06 §3), resolving each entry's own declared `secret_ref` at
    this boundary (12 §6's tools/models exception) — never inline in config,
    never passed to the agent.

    A provider this branch has no adapter for (`UnsupportedModelProvider`) is
    logged and skipped rather than aborting startup — 06 §4's provider
    isolation applied to configuration itself: one bad model-tool entry does
    not take down every other tool.
    """

    bootstrap_audit = _StartupAuditSink()

    for entry in config.models_as_tools:
        if not entry.enabled:
            continue

        api_key: str | None = None
        if entry.secret_ref:
            try:
                api_key = await security.secret_store.get(
                    session, entry.secret_ref, SecretRequester.server(), bootstrap_audit
                )
            except SecretStoreError:
                logger.error("model-tool %r: could not resolve secret_ref; skipped", entry.id)
                continue

        try:
            provider = build_provider(
                ModelProviderName(entry.provider), model=entry.model, endpoint=None, api_key=api_key
            )
        except (UnsupportedModelProvider, ValueError):
            logger.warning("model-tool %r: provider %r has no adapter; skipped", entry.id, entry.provider)
            continue

        executor = ModelToolExecutor(tool_id=entry.id, provider=provider)
        contract = ToolContract(
            tool_id=entry.id,
            version="1",
            description=f"Model-tool backed by {entry.provider}:{entry.model} (06 §3).",
            input_schema={"type": "object", "properties": {"prompt": {"type": "string"}}},
            output_schema={"type": "string"},
            required_capability=MODEL_TOOL_CAPABILITY,
            resource_scope={"model_tool_id": entry.id},
            network={"declared_destinations": ["configured model provider endpoint"]},
            filesystem={},
            risk_category=RiskCategory.LOW_WRITE,
            timeout_seconds=30,
            confirmation_required=False,
            failure_behavior="observation",
            audit="every invocation",
        )
        runtime.tool_registry.register(
            contract,
            ToolConfiguration(tool_id=entry.id, enabled=True, secret_ref=entry.secret_ref),
            executor,
        )


class _StartupAuditSink(SecretAuditSink):
    """A minimal `SecretAuditSink` for the one moment (app startup) before any
    request/`AuditLogger` exists. Logs rather than persisting — there is no
    request-scoped transaction to attach a startup-time secret resolution to,
    and 12 §5 requires *some* audit trail, so this is that trail's floor."""

    async def record_secret_event(self, event) -> None:  # noqa: ANN001
        logger.info("startup secret resolution: %s ref=%s result=%s", event.action, event.secret_ref, event.result)


# ── per-request adapters ─────────────────────────────────────────────────────


class EngineAuthorizer:
    """Structurally satisfies `server.agent.ports.Authorizer`.

    The one place `AuthorizationEngine.authorize()` and
    `ConfirmationService.issue()` are actually called on the runtime's
    behalf — `server.agent` cannot import either (INV-8), so this class is
    where "the runtime -> authz path" (16 §5's own phrase) is realized.
    """

    def __init__(
        self,
        *,
        engine: AuthorizationEngine,
        confirmations: ConfirmationService,
        session: AsyncSession,
        audit: AuditLogger,
    ) -> None:
        self._engine = engine
        self._confirmations = confirmations
        self._session = session
        self._audit = audit

    async def authorize(self, request: AccessRequest) -> RuntimeAuthorizationResult:
        outcome = await self._engine.authorize(self._session, request, audit=self._audit)

        issued_token: str | None = None
        if outcome.needs_confirmation and outcome.confirmation_required_for is not None:
            try:
                issued = await self._confirmations.issue(
                    self._session,
                    binding=outcome.confirmation_required_for,
                    risk_category=outcome.risk_category,
                )
                issued_token = issued.token
                await self._audit.record(
                    actor=AuditActor.AGENT,
                    action=AuditAction.CONFIRMATION_ISSUED,
                    resource=f"agent_task:{request.task_id}",
                    result=AuditResult.SUCCESS,
                    user_id=request.principal.user_id,
                    device_id=request.principal.device_id,
                    session_id=request.principal.session_id,
                    graph_id=request.graph_id,
                )
            except (AbsoluteFloorViolation, ConfirmationRefused):
                # 04 §2's own ordering already keeps a floor action from ever
                # reaching `needs_confirmation` (see
                # `server/graph/authorization.py`'s absolute-floor gate,
                # checked before any risk tier is computed) — this is
                # defense in depth, not a path this branch expects to take.
                logger.error(
                    "confirmation issuance refused for a require_confirmation outcome "
                    "(task=%s) — treating as no token issued", request.task_id
                )

        return RuntimeAuthorizationResult(outcome=outcome, issued_confirmation_token=issued_token)


class RegistryToolDispatcher:
    """Structurally satisfies `server.agent.ports.ToolDispatcher`.

    `allowed_tool_ids` is this principal's resolved `AgentConfiguration.
    enabled_tools`/`model_tools` (06 §3 MP-T4: "the agent cannot invoke a
    model-tool that isn't in its resolved config") — enforced here, at
    dispatch, not only at the `ToolCatalog` discovery layer, because
    discovery filtering alone would not stop a proposal naming a tool that is
    globally registered+enabled but simply not this principal's to use.
    """

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        allowed_tool_ids: frozenset[str],
        session: AsyncSession,
        audit: AuditLogger,
        principal: Principal,
    ) -> None:
        self._registry = registry
        self._allowed = allowed_tool_ids
        self._session = session
        self._audit = audit
        self._principal = principal

    async def dispatch(self, request: ToolInvocationRequest) -> ToolResult:
        if request.tool_id not in self._allowed:
            await self._audit.record(
                actor=AuditActor.AGENT,
                action=AuditAction.AGENT_TOOL_FAILED,
                resource=f"tool:{request.tool_id}",
                result=AuditResult.BLOCKED,
                user_id=self._principal.user_id,
                device_id=self._principal.device_id,
                session_id=self._principal.session_id,
            )
            raise ToolExecutionFailed(
                f"tool {request.tool_id!r} is not in this agent's resolved configuration"
            )

        try:
            result = await self._registry.dispatch(request)
        except ToolExecutionFailed:
            await self._audit.record(
                actor=AuditActor.AGENT,
                action=AuditAction.AGENT_TOOL_FAILED,
                resource=f"tool:{request.tool_id}",
                result=AuditResult.FAILURE,
                user_id=self._principal.user_id,
                device_id=self._principal.device_id,
                session_id=self._principal.session_id,
            )
            raise

        self._session.add(
            UsageEventRow(
                request_id=self._audit.request_id,
                user_id=self._principal.user_id,
                device_id=self._principal.device_id,
                session_id=self._principal.session_id,
                kind=UsageKind.TOOL_CALL,
                tool_id=request.tool_id,
                tokens_or_units=result.tokens_or_units,
                estimated_cost=result.estimated_cost,
                timestamp=_utcnow(),
            )
        )
        await self._session.flush()

        await self._audit.record(
            actor=AuditActor.AGENT,
            action=AuditAction.AGENT_TOOL_INVOKED if result.success else AuditAction.AGENT_TOOL_FAILED,
            resource=f"tool:{request.tool_id}",
            result=AuditResult.SUCCESS if result.success else AuditResult.FAILURE,
            user_id=self._principal.user_id,
            device_id=self._principal.device_id,
            session_id=self._principal.session_id,
        )
        return result


class RegistryToolCatalog:
    """Structurally satisfies `server.agent.ports.ToolCatalog`."""

    def __init__(self, *, registry: ToolRegistry, allowed_tool_ids: frozenset[str]) -> None:
        self._registry = registry
        self._allowed = allowed_tool_ids

    async def list_enabled(self) -> list[ToolContract]:
        return [c for c in self._registry.list_enabled_contracts() if c.tool_id in self._allowed]


class ModelInvokerAdapter:
    """Structurally satisfies `server.agent.ports.ModelInvoker`.

    Wraps a concrete `server.models.provider.ModelProvider` (or `None`, when
    no adapter is registered for the configured provider — see
    `resolve_agent_configuration`'s caller) and writes the `UsageEvent(kind=
    model_call)` USAGE-001 requires after every successful call.
    """

    def __init__(
        self,
        *,
        provider: object | None,
        provider_name: str,
        model_name: str,
        session: AsyncSession,
        audit: AuditLogger,
        principal: Principal,
    ) -> None:
        self._provider = provider
        self._provider_name = provider_name
        self._model_name = model_name
        self._session = session
        self._audit = audit
        self._principal = principal

    async def invoke(
        self, messages: list[ModelMessage], policy: GenerationPolicy, timeout: float
    ) -> ModelResult:
        if self._provider is None:
            raise ModelUnavailable(
                f"no ModelProvider adapter is registered for {self._provider_name!r} (FAIL-CORE-002)"
            )

        result = await self._provider.invoke(messages, policy, timeout)  # type: ignore[attr-defined]

        self._session.add(
            UsageEventRow(
                request_id=self._audit.request_id,
                user_id=self._principal.user_id,
                device_id=self._principal.device_id,
                session_id=self._principal.session_id,
                kind=UsageKind.MODEL_CALL,
                provider=self._provider_name,
                model=self._model_name,
                # `[IMPL]` cost estimation per provider/model is not
                # implemented by this branch — recorded honestly as 0.0
                # (never fabricated) rather than a guessed figure; a future
                # branch wiring real per-token pricing tables fills this in.
                tokens_or_units=result.tokens_used,
                estimated_cost=0.0,
                timestamp=_utcnow(),
            )
        )
        await self._session.flush()
        return result

    async def health(self) -> bool:
        if self._provider is None:
            return False
        return await self._provider.health()  # type: ignore[attr-defined]


_PERSISTED_EVENT_ACTIONS: dict[RuntimeEventKind, tuple[AuditAction, AuditActor, AuditResult]] = {
    RuntimeEventKind.TASK_CREATED: (AuditAction.AGENT_TASK_CREATED, AuditActor.AGENT, AuditResult.SUCCESS),
    RuntimeEventKind.TASK_COMPLETED: (AuditAction.AGENT_TASK_COMPLETED, AuditActor.AGENT, AuditResult.SUCCESS),
    RuntimeEventKind.TASK_FAILED: (AuditAction.AGENT_TASK_FAILED, AuditActor.AGENT, AuditResult.FAILURE),
    RuntimeEventKind.TASK_CANCELLED: (AuditAction.AGENT_TASK_CANCELLED, AuditActor.AGENT, AuditResult.SUCCESS),
    RuntimeEventKind.CONFIRMATION_ACCEPTED: (
        AuditAction.CONFIRMATION_ACCEPTED,
        AuditActor.USER,
        AuditResult.SUCCESS,
    ),
    RuntimeEventKind.CONFIRMATION_REJECTED: (
        AuditAction.CONFIRMATION_REJECTED,
        AuditActor.USER,
        AuditResult.BLOCKED,
    ),
}


class GatewayEventRecorder:
    """Structurally satisfies `server.agent.ports.EventRecorder`.

    Persists a curated subset of `RuntimeEventKind` as real `AuditEvent` rows
    (this branch's §13 instruction, satisfied without `server.agent` ever
    touching `server.storage`); everything else is observability-only —
    either genuinely not security-sensitive (`MODEL_PROPOSAL`) or already
    persisted by the component that has the authoritative detail
    (`AUTHORIZATION_REQUESTED`/`DECIDED` by the engine itself inside
    `authorize()`; `TOOL_INVOCATION`/`TOOL_RESULT` by `RegistryToolDispatcher`,
    which has the actual success/failure outcome this event does not carry).
    `CONFIRMATION_REQUESTED` is likewise skipped here — `EngineAuthorizer`
    already records `CONFIRMATION_ISSUED` at the moment a token exists to
    audit, which this event (raised slightly later, once the runtime has
    also updated task state) would only duplicate.
    """

    def __init__(self, *, audit: AuditLogger, principal: Principal) -> None:
        self._audit = audit
        self._principal = principal

    async def record(self, event: AgentEvent) -> None:
        mapping = _PERSISTED_EVENT_ACTIONS.get(event.kind)
        if mapping is None:
            logger.debug("runtime event %s task=%s data=%s", event.kind.value, event.task_id, event.data)
            return

        action, actor, result = mapping
        await self._audit.record(
            actor=actor,
            action=action,
            resource=f"agent_task:{event.task_id}",
            result=result,
            user_id=self._principal.user_id,
            device_id=self._principal.device_id,
            session_id=self._principal.session_id,
        )


# ── AgentConfiguration resolution (05 §5, OD-RT-3) ──────────────────────────


async def resolve_agent_configuration(
    session: AsyncSession, *, principal: Principal, app_config: AppConfig
) -> AgentConfiguration:
    """OD-RT-3 `[IMPL, constrained: must be deterministic + documented]`.

    **This branch's resolution choice:** a user-scoped `AgentConfiguration`
    takes precedence over a graph-scoped one when both exist for the
    principal's active graph. Rationale: a member's own model/tool
    preference is theirs, set for themselves, and should not be silently
    overridden merely because they are acting inside a shared graph that
    happens to have its own configuration — the opposite precedence would
    mean joining a graph could change which model runs on a user's own
    behalf without their consent. Falls back to `AppConfig.agent`'s
    server-wide default when neither scope has a stored configuration (the
    common case before any user has customized anything).

    Read-only: this function creates nothing. `01` §4.1's own text already
    treats a missing config as "use the server default", so an absent row is
    not an error.
    """

    result = await session.execute(
        select(AgentConfigurationRow).where(
            AgentConfigurationRow.scope_type == AgentConfigScopeType.USER,
            AgentConfigurationRow.scope_id == principal.user_id,
        )
    )
    row = result.scalars().first()

    if row is None and principal.active_graph_id is not None:
        result = await session.execute(
            select(AgentConfigurationRow).where(
                AgentConfigurationRow.scope_type == AgentConfigScopeType.GRAPH,
                AgentConfigurationRow.scope_id == principal.active_graph_id,
            )
        )
        row = result.scalars().first()

    if row is not None:
        return AgentConfiguration.model_validate(row)

    return AgentConfiguration(
        scope_type=AgentConfigScopeType.USER,
        scope_id=principal.user_id,
        primary_model=ModelConfiguration(
            provider=app_config.agent.provider,
            model=app_config.agent.model,
            secret_ref=app_config.agent.secret_ref,
            timeout_seconds=int(app_config.agent.bounds.model_call_timeout_seconds),
            generation_policy=app_config.agent.generation_policy or None,
        ),
        model_tools=[entry.id for entry in app_config.models_as_tools if entry.enabled],
        enabled_tools=[entry.tool_id for entry in app_config.tools if entry.enabled],
        granted_capabilities=None,
    )


# ── per-request orchestrator assembly ───────────────────────────────────────


async def build_agent_orchestrator(
    *,
    session: AsyncSession,
    audit: AuditLogger,
    principal: Principal,
    security: SecurityCore,
    runtime: RuntimeCore,
    app_config: AppConfig,
) -> AgentOrchestrator:
    """The one function that assembles a request's `AgentOrchestrator` from
    Security Core + the runtime's own long-lived state. Called once per
    `/agent/tasks*` request (`server/gateway/routers/agent.py`)."""

    agent_config = await resolve_agent_configuration(session, principal=principal, app_config=app_config)
    allowed_tool_ids = frozenset(
        (agent_config.enabled_tools or []) + (agent_config.model_tools or [])
    )

    api_key: str | None = None
    if agent_config.primary_model.secret_ref:
        api_key = await security.secret_store.get(
            session, agent_config.primary_model.secret_ref, SecretRequester.server(), audit
        )

    try:
        provider = build_provider(
            ModelProviderName(agent_config.primary_model.provider),
            model=agent_config.primary_model.model,
            endpoint=agent_config.primary_model.endpoint,
            api_key=api_key,
        )
    except (UnsupportedModelProvider, ValueError):
        provider = None

    return AgentOrchestrator(
        principal=principal,
        bounds=runtime.bounds,
        authorizer=EngineAuthorizer(
            engine=security.engine, confirmations=security.confirmations, session=session, audit=audit
        ),
        model=ModelInvokerAdapter(
            provider=provider,
            provider_name=agent_config.primary_model.provider.value,
            model_name=agent_config.primary_model.model,
            session=session,
            audit=audit,
            principal=principal,
        ),
        tools=RegistryToolDispatcher(
            registry=runtime.tool_registry,
            allowed_tool_ids=allowed_tool_ids,
            session=session,
            audit=audit,
            principal=principal,
        ),
        memory=NullMemoryHydrator(),
        events=GatewayEventRecorder(audit=audit, principal=principal),
        tool_catalog=RegistryToolCatalog(registry=runtime.tool_registry, allowed_tool_ids=allowed_tool_ids),
        task_store=runtime.task_store,
    )


__all__ = [
    "EngineAuthorizer",
    "GatewayEventRecorder",
    "ModelInvokerAdapter",
    "RegistryToolCatalog",
    "RegistryToolDispatcher",
    "RuntimeCore",
    "build_agent_orchestrator",
    "build_runtime_core",
    "populate_tool_registry",
    "resolve_agent_configuration",
]
