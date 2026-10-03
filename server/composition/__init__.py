"""The composition root — the one place the whole server is assembled.

16 §2 layers the server so that dependencies point inward toward deterministic
control. That makes two edges impossible to express from inside any layer:

* the gateway (control & policy) must host the agent endpoints, but may not
  import the agent runtime (application), and
* the agent runtime must consult the authorization engine, but may not import
  it (16 §5 / INV-8 — "agent cannot import capabilities").

Both are resolved the standard way: each lower module declares a Protocol for
what it needs (`server/gateway/agent_port.py`, `server/agent/ports.py`), and
this package — above every layer, imported by nothing but the process
entrypoint — wires the real objects together. It adds no policy of its own.
"""

from __future__ import annotations

from typing import Iterable

import httpx

from fastapi import FastAPI

from server.agent import AgentRuntime, ConcurrencyGate, ConcurrencyLimits, RuntimeBounds
from server.agent.breaker import BreakerLimits
from server.agent.recovery import RecoveryPolicy
from server.agents.service import AgentDefinitionService
from server.composition.agent_triggers import AgentTriggerLoop
from server.composition.model_gateway import HttpModelGateway, ModelGatewayListener
from server.composition.agents import (
    AgentDefinitionLoader,
    AgentFactory,
    AgentFactoryFacade,
    AgentReminders,
    AgentRunCoordinator,
    agent_tool_definitions,
    audit_delegation_ended,
    registries_from_config,
)
from server.composition.break_glass import BreakGlassRegistry
from server.composition.execution_tools import build_execution_tools
from server.composition.console import build_console
from server.composition.evaluation import build_evaluation
from server.composition.facade import AgentTaskFacade
from server.composition.improvements import EvaluationControl, EvaluationSwitchboard, TuningCache
from server.composition.latch import InProcessLatch
from server.composition.supervisor import SupervisorControl
from server.evaluation.provider import EvaluationProvider
from server.execution.device_hub import DeviceHub
from server.composition.models import ProviderFactory, spec_from_entry
from server.composition.push import attach_push_wake
from server.composition.secret_context import key_provider_for
from server.config.errors import ConfigError
from server.config.schema import AppConfig
from server.gateway.app import create_app
from server.gateway.security import SecurityCore, build_security_core
from server.composition.memory import MemoryFacade, MemoryFactLoader, VaultFacade
from server.composition.voice import build_voice
from server.composition.scheduler import (
    HubReminderChannel,
    HubWake,
    ReminderInboxAdapter,
    SchedulerFacade,
    SecurityCoreFireChecks,
    reminder_tool_definition,
)
from server.scheduler.firing import ReminderFirer, SchedulerRunner
from server.scheduler.backend import build_backend
from server.scheduler.service import SchedulerService
from server.security.usage import SchedulerLimits, SchedulerQuota
from server.memory.hydration import AuthorizedContextHydrator
from server.memory.provider import MemoryProvider, MemoryStore
from server.vault.index import VaultIndex
from server.models.factory import build_provider
from server.modeltools import model_tool_definition
from server.secrets.requester import SecretRequester
from server.security.usage import UsageLimits, UsagePolicy
from server.storage import StorageBackend
from server.tools.device_vision import ScreenshotVision
from server.tools.registry import ToolDefinition, ToolRegistry
from shared.schemas.authorization import ResourceType


def bounds_from_config(config: AppConfig) -> RuntimeBounds:
    b = config.agent.bounds
    return RuntimeBounds(
        max_iterations=b.max_iterations,
        max_model_calls=b.max_model_calls,
        max_tool_calls=b.max_tool_calls,
        max_model_tool_nesting_depth=b.max_model_tool_nesting_depth,
        wall_clock_timeout_seconds=b.wall_clock_timeout_seconds,
        per_task_budget=b.per_task_budget,
        max_parse_retries=b.max_parse_retries,
        max_input_chars=b.max_input_chars,
        max_observation_chars=b.max_observation_chars,
        max_context_chars=b.max_context_chars,
    )


def breaker_limits_from_config(config: AppConfig) -> BreakerLimits:
    b = config.agent.breaker
    return BreakerLimits(
        denial_limit=b.denial_limit,
        violation_limit=b.violation_limit,
        rejection_limit=b.rejection_limit,
    )


def recovery_from_config(config: AppConfig) -> RecoveryPolicy | None:
    """18 §4/§9. `None` — no `agent.recovery` section — keeps today's
    behaviour exactly."""

    r = config.agent.recovery
    if r is None:
        return None
    return RecoveryPolicy(
        max_worker_switches=r.max_worker_switches,
        escalate_on_unresolved=r.escalate_on_unresolved,
        stall_window=r.stall_window,
        loop_repeat_limit=r.loop_repeat_limit,
    )


def usage_limits_from_config(config: AppConfig) -> UsageLimits:
    rates = config.security.rate_limits
    budgets = config.security.budgets
    return UsageLimits(
        per_user_calls_per_minute=rates.per_user_requests_per_minute,
        per_device_calls_per_minute=rates.per_device_requests_per_minute,
        global_calls_per_minute=rates.global_requests_per_minute,
        per_user_daily_cost_limit=budgets.per_user_daily_cost_limit,
        global_daily_cost_limit=budgets.global_daily_cost_limit,
        evaluation_counts_toward_global_budget=config.evaluation.budget_scope == "own_and_global",
    )


def scheduler_service_from_config(config: AppConfig) -> SchedulerService:
    """docs/22 §3/§4: the configured backend and the per-user quota."""

    return SchedulerService(
        config=config.scheduler,
        backend=build_backend(config.scheduler.backend),
        quota=SchedulerQuota(
            SchedulerLimits(
                max_active_jobs_per_user=config.scheduler.max_active_jobs_per_user,
                creations_per_hour=config.security.rate_limits.scheduler_creations_per_hour,
            )
        ),
    )


def concurrency_from_config(config: AppConfig) -> ConcurrencyLimits:
    rates = config.security.rate_limits
    return ConcurrencyLimits(
        per_session=rates.per_session_concurrent_tasks,
        per_user=rates.per_user_concurrent_tasks,
        global_=rates.global_concurrent_tasks,
    )


def _screenshot_vision(config: AppConfig, factory: ProviderFactory) -> ScreenshotVision | None:
    """docs/23 §6 level 4: the configured vision model, resolving its own
    declared `secret_ref` at call time like a model-tool — or none."""

    entry = config.android.vision
    if not config.android.enabled or entry is None:
        return None
    provider = factory(spec_from_entry(entry), key_provider=key_provider_for(entry.secret_ref, SecretRequester.server()))
    return ScreenshotVision(provider)


def build_tool_registry(
    config: AppConfig,
    *,
    provider_factory: ProviderFactory = build_provider,
    extra_tools: Iterable[ToolDefinition] = (),
) -> ToolRegistry:
    """Register configured model-tools and any supplied definitions.

    `extra_tools` is the seam a later branch (filesystem, network, device) — or
    a test — uses to register its tools; every one goes through the same
    `ToolRegistry.register` validation. A `tools:` config entry that enables a
    tool id with no registered implementation is a startup failure (TOOL-003),
    not a silently missing tool.
    """

    registry = ToolRegistry()
    for entry in config.models_as_tools:
        provider = provider_factory(
            spec_from_entry(entry),
            key_provider=key_provider_for(entry.secret_ref, SecretRequester.server()),
        )
        parts = model_tool_definition(entry.id, description=entry.description, provider=provider)
        registry.register(ToolDefinition(**vars(parts)), enabled=entry.enabled)

    enabled_by_config = {t.tool_id: t.enabled for t in config.tools}
    for definition in extra_tools:
        registry.register(
            definition, enabled=enabled_by_config.get(definition.contract.tool_id, True)
        )

    for tool_id, enabled in enabled_by_config.items():
        if enabled and registry.resolve(tool_id) is None:
            raise ConfigError(
                f"tools: {tool_id!r} is enabled but no such tool is registered (TOOL-003)"
            )
    return registry


def build_application(
    config: AppConfig,
    *,
    storage: StorageBackend | None = None,
    security: SecurityCore | None = None,
    unlock_secrets_on_startup: bool = False,
    reconcile_tasks_on_startup: bool = False,
    provider_factory: ProviderFactory | None = None,
    extra_tools: Iterable[ToolDefinition] | None = None,
    memory_store: MemoryStore | None = None,
    break_glass_registry: BreakGlassRegistry | None = None,
    memory_provider: MemoryProvider | None = None,
    vault_index: VaultIndex | None = None,
    scheduler_tool: bool | None = None,
    agent_tools: bool | None = None,
    voice_transport: "httpx.AsyncBaseTransport | None" = None,
    evaluation_provider: EvaluationProvider | None = None,
) -> FastAPI:
    """Assemble the full server: Security Core, runtime, tools, models, memory.

    `provider_factory`, `memory_provider`, `memory_store` and `vault_index` are
    injection seams, not test modes: production passes none of them and gets the
    configured model providers, the self-hosted Mem0 store when
    `memory.enabled`, and the vault index when `vault.enabled`. With memory
    disabled, hydration degrades explicitly (FAIL-008) and the memory endpoints
    answer `503`. `memory_store` supplies hydration alone (a read-only store with
    no API or formation), for tests of the hydration boundary.

    `scheduler_tool` registers the scheduler's agent tool (docs/22 §1) alongside
    the chosen tool set. `None` (production) means "whatever the config says"
    when `extra_tools` is `None`, and no — an explicit list fully substitutes
    the tool set, as below — when a list is passed.

    `break_glass_registry` lets a test share one record store with the
    execution tools it passes in `extra_tools`; production passes nothing.

    `extra_tools` changed meaning with the execution branch: passing `None`
    (the default — production passes nothing) now gets the real execution
    tools (`build_execution_tools`, from `ExecutionConfig`) rather than no
    tools at all, since `server.fs`/`server.net`/`server.execution` now
    exist to back them. A caller — a test, or a self-hoster who wants a
    different tool set entirely — still fully substitutes by passing an
    explicit list, exactly as `extra_tools=()` did before.
    """

    core = security or build_security_core(config)
    factory = provider_factory or build_provider
    # docs/29 §24: the Agent Factory's registries are validated at every
    # startup, fail-closed, whether or not `agents.enabled`.
    agent_registries = registries_from_config(config)
    # 20 §2: the one store of break-glass records, shared by the executor
    # (claim), the runtime's security adapter (settle/end) and the superuser
    # control path (activate/revoke). Inert unless the operator enabled it.
    break_glass = break_glass_registry or BreakGlassRegistry(config.execution.process.break_glass)
    # docs/23 §4: one hub is both the Android tools' transport and the
    # gateway's device channel — built only when the operator enabled it.
    device_hub = DeviceHub() if config.android.enabled else None
    tool_definitions = (
        build_execution_tools(
            config, break_glass=break_glass, device_transport=device_hub,
            screenshot_vision=_screenshot_vision(config, factory),
        )
        if extra_tools is None
        else extra_tools
    )
    # docs/22: the scheduler. Its agent tool joins whatever tool set was chosen
    # (the same registry validation), so the capability path is the one every
    # other tool takes.
    scheduler_service = scheduler_service_from_config(config) if config.scheduler.enabled else None
    include_scheduler_tool = (extra_tools is None) if scheduler_tool is None else scheduler_tool
    if scheduler_service is not None and config.scheduler.agent_tool_enabled and include_scheduler_tool:
        tool_definitions = [*tool_definitions, reminder_tool_definition(scheduler_service)]
    # docs/29: the Agent Factory — nothing at all unless `agents.enabled`. Its
    # tools join the chosen tool set through the same registry validation, and
    # its resource type is decided by the same engine as every other.
    agent_factory: AgentFactory | None = None
    if config.agents.enabled:
        agent_factory = AgentFactory(
            config=config, core=core,
            service=AgentDefinitionService(
                agent_registries, preview_ttl_minutes=config.agents.compile_preview_ttl_minutes,
                max_agents_per_user=config.agents.max_agents_per_user,
                unattended_available=config.agents.unattended_enabled,
                delegation_ended=audit_delegation_ended,
            ),
        )
        core.resource_loader.register(ResourceType.AGENTDEFINITION, AgentDefinitionLoader())
        if scheduler_service is not None:
            # docs/29 §17.1 (Phase 4): reminder triggers become ordinary jobs.
            agent_factory.reminders = AgentReminders(scheduler=scheduler_service, core=core)
        include_agent_tools = (extra_tools is None) if agent_tools is None else agent_tools
        if include_agent_tools:
            tool_definitions = [*tool_definitions, *agent_tool_definitions(agent_factory)]
    tools = build_tool_registry(config, provider_factory=factory, extra_tools=tool_definitions)

    provider = memory_provider
    if provider is None and memory_store is None and config.memory.enabled:
        provider = _open_memory_provider(config)
    memory_facade: MemoryFacade | None = None
    if provider is not None:
        # The engine decides `mem0fact` operations like any other resource;
        # this is the projection it decides on (04 §1).
        core.resource_loader.register(ResourceType.MEM0FACT, MemoryFactLoader(provider))
        memory_facade = MemoryFacade(provider=provider, core=core, config=config.memory)

    index = vault_index
    if index is None and config.vault.enabled:
        index = _open_vault_index(config)
    vault_facade = VaultFacade(index=index, config=config.vault) if index is not None else None

    async def active_graph_ids(session, user_id):
        # H-1: read in a short transaction of its own, so none is left open
        # across the memory search that follows. The runtime has committed the
        # request's writes before hydrating, so this sees the same state.
        async with storage.session() as reads:
            graphs = await core.graph_repository.graphs_for_user(reads, user_id=user_id, limit=1000)
        return {g.graph_id for g in graphs}

    # docs/22 §2/§3: firing and delivery. The runner is a lifespan service; it
    # needs the same storage the app serves from, so that is fixed here.
    if storage is None:
        from server.storage import SQLAlchemyStorageBackend

        storage = SQLAlchemyStorageBackend(config.database_url)
    reminder_firer: ReminderFirer | None = None
    scheduler_runner = None
    background = []
    if scheduler_service is not None:
        reminder_firer = ReminderFirer(
            storage=storage, backend=scheduler_service.backend, config=config.scheduler,
            checks=SecurityCoreFireChecks(core),
            channel=HubReminderChannel(device_hub) if device_hub is not None else None,
            wake=HubWake(device_hub) if device_hub is not None else None,
        )
        scheduler_runner = SchedulerRunner(reminder_firer, poll_seconds=config.scheduler.poll_seconds)
        background.append(scheduler_runner)

    usage_policy = UsagePolicy(limits=usage_limits_from_config(config))
    latch = InProcessLatch()
    # 19 §9: approved configuration versions, and the Judge's switches (each
    # capped by `evaluation.*`). Both exist whether or not the Judge is on, so
    # an approved change keeps applying after the Judge is switched off.
    tuning = TuningCache(storage)
    switchboard = EvaluationSwitchboard(config.evaluation)
    runtime = AgentRuntime(
        bounds=bounds_from_config(config),
        concurrency=ConcurrencyGate(concurrency_from_config(config)),
        tools=tools,
        breaker_limits=breaker_limits_from_config(config),
        recovery=recovery_from_config(config),
    )
    facade = AgentTaskFacade(
        runtime=runtime,
        core=core,
        config=config,
        usage_policy=usage_policy,
        tools=tools,
        hydrator=AuthorizedContextHydrator(
            store=provider if provider is not None else memory_store,
            active_graph_ids=active_graph_ids,
            top_k=config.agent.bounds.memory_top_k,
        ),
        provider_factory=factory,
        latch=latch,
        break_glass=break_glass,
        memory=memory_facade,
        vault=vault_facade,
        tuning=tuning,
        agent_runs=(
            (lambda session, audit: AgentRunCoordinator(agent_factory, session, audit, core))
            if agent_factory is not None else None
        ),
        # docs/29 §15.2 (Phase 5): only with unattended runs switched on can a
        # delegated principal ever be fresh.
        delegations=(
            agent_factory.delegated_principal_active
            if agent_factory is not None and config.agents.unattended_enabled else None
        ),
    )
    # docs/29 §15.5 (Phase 5): the unattended trigger loop — the Agent
    # Factory's own, not the scheduler's; nothing at all unless the operator
    # switched unattended runs on.
    agent_triggers: AgentTriggerLoop | None = None
    if agent_factory is not None and config.agents.unattended_enabled:
        agent_triggers = AgentTriggerLoop(
            factory=agent_factory, tasks=facade, storage=storage,
            grace_minutes=config.agents.misfire_grace_minutes,
            interval_seconds=config.agents.trigger_interval_seconds,
        )
        background.append(agent_triggers)
    # docs/29 §12 (Phase 6, slice 6A): the HTTP Model Gateway an external
    # runtime calls — a separate app on an internal binding, served by its own
    # listener, never mounted on the public API; nothing at all unless the
    # operator switched it on.
    model_gateway: HttpModelGateway | None = None
    model_gateway_app = None
    model_gateway_listener: ModelGatewayListener | None = None
    if agent_factory is not None and config.agents.model_gateway.enabled:
        from server.gateway.routers.model_gateway import build_model_gateway_app

        model_gateway = HttpModelGateway(factory=agent_factory, storage=storage, core=core, usage=usage_policy,
                                         latch=latch, config=config, provider_factory=factory)
        model_gateway_app = build_model_gateway_app(
            model_gateway, max_request_bytes=config.agents.model_gateway.max_request_bytes)
        assert config.agents.model_gateway.listen is not None
        model_gateway_listener = ModelGatewayListener(app=model_gateway_app,
                                                      binding=config.agents.model_gateway.listen)
        background.append(model_gateway_listener)
    # 19: the Judge — nothing at all unless `evaluation.enabled`.
    evaluation = build_evaluation(
        config, runtime=runtime, storage=storage, factory=factory, tuning=tuning, switches=switchboard,
        environment=facade.environment, break_glass=break_glass, secret_store=core.secret_store,
        judge=evaluation_provider,
    )
    evaluation_jobs = None
    if evaluation is not None:
        observer, evaluation_jobs = evaluation
        runtime.attach_observer(observer)
        background.append(evaluation_jobs.queue)
    app = create_app(
        config=config,
        storage=storage,
        security=core,
        unlock_secrets_on_startup=unlock_secrets_on_startup,
        reconcile_tasks_on_startup=reconcile_tasks_on_startup,
        agent_tasks=facade,
        agent_factory=(
            AgentFactoryFacade(factory=agent_factory, core=core, tasks=facade) if agent_factory is not None else None
        ),
        # 18 §5.4: the operator control path. Reached only through
        # `/api/v1/admin/control/*`, behind `get_superuser`.
        supervisor_control=SupervisorControl(runtime=runtime, facade=facade, latch=latch,
                                             break_glass=break_glass, agents=agent_factory),
        memory_port=memory_facade,
        vault_port=vault_facade,
        device_hub=device_hub,
        scheduler_port=(
            SchedulerFacade(service=scheduler_service, core=core) if scheduler_service is not None else None
        ),
        reminder_inbox=ReminderInboxAdapter(reminder_firer) if reminder_firer is not None else None,
        background=background,
        # docs/27: voice is detachable — the facade exists either way, and a
        # direction placed on the device or off simply has no server provider.
        voice_port=build_voice(config, core=core, usage=usage_policy, transport=voice_transport),
        # 19 §9 / 28 §1: the Judge's control path — superuser only, owned here,
        # not by the dashboard.
        evaluation_control=EvaluationControl(switchboard=switchboard, tuning=tuning, storage=storage),
        # 28: the operator console — read-only views over snapshots.
        operator_console=build_console(
            config, runtime=runtime, latch=latch, break_glass=break_glass, core=core, factory=factory,
            memory=memory_facade, provider=provider, vault=vault_facade, device_hub=device_hub,
            scheduler_runner=scheduler_runner, switchboard=switchboard, jobs=evaluation_jobs, storage=storage,
        ),
    )
    app.state.evaluation = evaluation_jobs
    app.state.agent_triggers = agent_triggers
    app.state.model_gateway = model_gateway
    app.state.model_gateway_app = model_gateway_app
    app.state.model_gateway_listener = model_gateway_listener
    # docs/23 §4: the optional push wake — nothing at all unless configured.
    attach_push_wake(config, app, device_hub)
    return app


def _open_memory_provider(config: AppConfig) -> MemoryProvider:
    """`memory.enabled` is an operator's explicit request, so a stack that cannot
    start safely stops the server rather than silently running without memory."""

    from server.memory.mem0_provider import Mem0SetupError, build_mem0_provider
    from server.models.embedding import EmbedderUnavailable

    try:
        return build_mem0_provider(config.memory.mem0)
    except (Mem0SetupError, EmbedderUnavailable) as exc:
        raise ConfigError(f"memory.enabled is true but the memory store cannot start: {exc}") from None


def _open_vault_index(config: AppConfig) -> VaultIndex:
    from server.models.embedding import EmbedderUnavailable
    from server.vault.index import open_vault_index

    try:
        return open_vault_index(config.vault)
    except EmbedderUnavailable as exc:
        raise ConfigError(f"vault.enabled is true but the vault index cannot start: {exc}") from None


__all__ = [
    "bounds_from_config",
    "build_application",
    "build_tool_registry",
    "concurrency_from_config",
    "scheduler_service_from_config",
    "usage_limits_from_config",
]
