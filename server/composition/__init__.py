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

from fastapi import FastAPI

from server.agent import AgentRuntime, ConcurrencyGate, ConcurrencyLimits, RuntimeBounds
from server.agent.breaker import BreakerLimits
from server.composition.execution_tools import build_execution_tools
from server.composition.facade import AgentTaskFacade
from server.composition.latch import InProcessLatch
from server.composition.supervisor import SupervisorControl
from server.composition.models import ProviderFactory, spec_from_entry
from server.composition.secret_context import key_provider_for
from server.config.errors import ConfigError
from server.config.schema import AppConfig
from server.gateway.app import create_app
from server.gateway.security import SecurityCore, build_security_core
from server.memory.hydration import AuthorizedContextHydrator, MemoryStore
from server.models.factory import build_provider
from server.modeltools import model_tool_definition
from server.secrets.requester import SecretRequester
from server.security.usage import UsageLimits, UsagePolicy
from server.storage import StorageBackend
from server.tools.registry import ToolDefinition, ToolRegistry


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


def usage_limits_from_config(config: AppConfig) -> UsageLimits:
    rates = config.security.rate_limits
    budgets = config.security.budgets
    return UsageLimits(
        per_user_calls_per_minute=rates.per_user_requests_per_minute,
        per_device_calls_per_minute=rates.per_device_requests_per_minute,
        global_calls_per_minute=rates.global_requests_per_minute,
        per_user_daily_cost_limit=budgets.per_user_daily_cost_limit,
        global_daily_cost_limit=budgets.global_daily_cost_limit,
    )


def concurrency_from_config(config: AppConfig) -> ConcurrencyLimits:
    rates = config.security.rate_limits
    return ConcurrencyLimits(
        per_session=rates.per_session_concurrent_tasks,
        per_user=rates.per_user_concurrent_tasks,
        global_=rates.global_concurrent_tasks,
    )


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
    provider_factory: ProviderFactory | None = None,
    extra_tools: Iterable[ToolDefinition] | None = None,
    memory_store: MemoryStore | None = None,
) -> FastAPI:
    """Assemble the full server: Security Core, runtime, tools, models, memory.

    `provider_factory` and `memory_store` are injection seams, not test
    modes: production passes neither and gets the configured providers and
    — until `11` supplies a Mem0 store — explicit FAIL-008 degradation for
    long-term memory.

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
    tool_definitions = build_execution_tools(config) if extra_tools is None else extra_tools
    tools = build_tool_registry(config, provider_factory=factory, extra_tools=tool_definitions)

    async def active_graph_ids(session, user_id):
        graphs = await core.graph_repository.graphs_for_user(session, user_id=user_id, limit=1000)
        return {g.graph_id for g in graphs}

    latch = InProcessLatch()
    runtime = AgentRuntime(
        bounds=bounds_from_config(config),
        concurrency=ConcurrencyGate(concurrency_from_config(config)),
        tools=tools,
        breaker_limits=breaker_limits_from_config(config),
    )
    facade = AgentTaskFacade(
        runtime=runtime,
        core=core,
        config=config,
        usage_policy=UsagePolicy(limits=usage_limits_from_config(config)),
        tools=tools,
        hydrator=AuthorizedContextHydrator(
            store=memory_store,
            active_graph_ids=active_graph_ids,
            top_k=config.agent.bounds.memory_top_k,
        ),
        provider_factory=factory,
        latch=latch,
    )
    return create_app(
        config=config,
        storage=storage,
        security=core,
        unlock_secrets_on_startup=unlock_secrets_on_startup,
        agent_tasks=facade,
        # 18 §5.4: the operator control path. Reached only through
        # `/api/v1/admin/control/*`, behind `get_superuser`.
        supervisor_control=SupervisorControl(runtime=runtime, facade=facade, latch=latch),
    )


__all__ = [
    "bounds_from_config",
    "build_application",
    "build_tool_registry",
    "concurrency_from_config",
    "usage_limits_from_config",
]
