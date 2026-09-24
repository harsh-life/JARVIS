"""Resolving which model a task uses (01 §4.1, 06, OD-RT-3).

Precedence `[PROPOSED]` (OD-RT-3, docs/DECISION_REGISTER.md §2) — deterministic,
first match wins, never decided by the model:

    1. the user's own AgentConfiguration (scope_type = user)
    2. the task graph's AgentConfiguration (scope_type = graph), only if the
       principal is an active member of that graph
    3. the server configuration's `agent` section

A user's model choice — and its cost — is theirs: a graph owner cannot switch
another member's model. A configuration row may name a paid provider only if the
server configuration prices that exact provider+model; otherwise the task fails
explicitly (an unpriced paid call cannot be budgeted, 13 §3). A row never
chooses the endpoint either: that comes from the server configuration, so a
stored config cannot aim the server's outbound call at an arbitrary host.

`AgentConfiguration` is configuration, never authority: `enabled_tools` /
`model_tools` narrow which tools the agent may *see and call* (MP-T4); whether a
call is authorized is still the engine's decision against `CapabilityGrant`.
"""

from __future__ import annotations

import uuid
from typing import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.agent.ports import ResolvedModels
from server.composition.secret_context import key_provider_for
from server.config.schema import LOCAL_MODEL_PROVIDERS, AppConfig, ModelEntryConfig, ModelPricingConfig
from server.graph.repository import GraphRepository
from server.models.factory import ProviderNotImplemented, build_provider
from server.models.provider import ModelPricing, ModelProvider, ModelSpec, ModelUnavailable
from server.secrets.requester import SecretRequester
from server.storage.models import AgentConfiguration
from shared.schemas.agent_config import ModelConfiguration
from shared.schemas.authorization import Principal
from shared.schemas.enums import AgentConfigScopeType

ProviderFactory = Callable[..., ModelProvider]


def _pricing(config: ModelPricingConfig | None) -> ModelPricing:
    if config is None:
        return ModelPricing()
    return ModelPricing(config.input_per_1k_tokens, config.output_per_1k_tokens)


def spec_from_entry(entry) -> ModelSpec:
    """`entry` is the `agent` section or a `ModelEntryConfig`/`ModelToolEntryConfig`."""

    return ModelSpec(
        provider=entry.provider,
        model=entry.model,
        endpoint=entry.endpoint,
        timeout_seconds=float(entry.timeout_seconds),
        pricing=_pricing(entry.pricing),
        generation_policy=dict(entry.generation_policy or {}),
    )


class ConfiguredModelResolver:
    """Request-scoped `ModelResolverPort`."""

    def __init__(
        self,
        *,
        config: AppConfig,
        session: AsyncSession,
        graph_repository: GraphRepository,
        provider_factory: ProviderFactory = build_provider,
    ) -> None:
        self._config = config
        self._session = session
        self._graphs = graph_repository
        self._factory = provider_factory
        self._priced = self._server_priced_models(config)
        self._endpoints = self._server_endpoints(config)

    @staticmethod
    def _operator_fallbacks(config: AppConfig) -> list[ModelEntryConfig]:
        """18 §4.2: the operator-configured part of every chain, in order —
        `agent.fallback`, then `agent.recovery.chain`. Only the operator's
        configuration contributes here, so a worker one user configured is
        never used for another user's task."""

        entries: list[ModelEntryConfig] = []
        if config.agent.fallback is not None:
            entries.append(config.agent.fallback)
        if config.agent.recovery is not None:
            entries.extend(config.agent.recovery.chain)
        return entries

    @classmethod
    def _server_entries(cls, config: AppConfig) -> list:
        return [config.agent, *[m for m in config.models_as_tools], *cls._operator_fallbacks(config)]

    @classmethod
    def _server_priced_models(cls, config: AppConfig) -> dict[tuple[str, str], ModelPricing]:
        return {
            (e.provider, e.model): _pricing(e.pricing)
            for e in cls._server_entries(config)
            if e.pricing is not None
        }

    @classmethod
    def _server_endpoints(cls, config: AppConfig) -> dict[tuple[str, str], str]:
        return {
            (e.provider, e.model): e.endpoint
            for e in cls._server_entries(config)
            if e.endpoint
        }

    def _build(self, spec: ModelSpec, secret_ref: str | None, requester: SecretRequester) -> ModelProvider:
        try:
            return self._factory(spec, key_provider=key_provider_for(secret_ref, requester))
        except (ProviderNotImplemented, ValueError) as exc:
            raise ModelUnavailable(str(exc)) from None

    async def _config_row(
        self, principal: Principal, graph_id: uuid.UUID | None
    ) -> tuple[AgentConfiguration, SecretRequester] | None:
        row = (
            await self._session.execute(
                select(AgentConfiguration).where(
                    AgentConfiguration.scope_type == AgentConfigScopeType.USER,
                    AgentConfiguration.scope_id == principal.user_id,
                )
            )
        ).scalars().first()
        if row is not None:
            return row, SecretRequester.user(principal.user_id)

        if graph_id is not None and await self._graphs.is_active_member(
            self._session, graph_id=graph_id, user_id=principal.user_id
        ):
            row = (
                await self._session.execute(
                    select(AgentConfiguration).where(
                        AgentConfiguration.scope_type == AgentConfigScopeType.GRAPH,
                        AgentConfiguration.scope_id == graph_id,
                    )
                )
            ).scalars().first()
            if row is not None:
                # 12 §2: a graph-scoped secret resolves only for server-owned work
                # in that graph — never to a member as a user.
                return row, SecretRequester.server(graph_id=graph_id)
        return None

    async def resolve(self, *, principal: Principal, graph_id: uuid.UUID | None) -> ResolvedModels:
        fallbacks = tuple(
            self._build(spec_from_entry(entry), entry.secret_ref, SecretRequester.server())
            for entry in self._operator_fallbacks(self._config)
        )

        found = await self._config_row(principal, graph_id)
        if found is None:
            primary = self._build(
                spec_from_entry(self._config.agent), self._config.agent.secret_ref, SecretRequester.server()
            )
            return ResolvedModels(chain=(primary, *fallbacks))

        row, requester = found
        try:
            model_config = ModelConfiguration.model_validate(row.primary_model)
        except Exception:  # noqa: BLE001 — a malformed stored config fails the task explicitly
            raise ModelUnavailable("stored agent configuration is invalid") from None

        # A stored row names a SecretStore handle, never `env:NAME`. `env:` is the
        # operator's config-file vocabulary (15 §2): it reads the server's own
        # process environment with no requester, no scope, and no audit — where
        # the KEK source and the superuser token live. Accepting it from a row
        # would let a stored configuration ship either one to a provider as its
        # API key.
        if model_config.secret_ref and model_config.secret_ref.startswith("env:"):
            raise ModelUnavailable("a stored agent configuration may only reference a SecretStore handle")

        provider_name = model_config.provider.value
        pricing = self._priced.get((provider_name, model_config.model))
        if provider_name not in LOCAL_MODEL_PROVIDERS and pricing is None:
            raise ModelUnavailable(
                f"{provider_name}:{model_config.model} is not priced by the server configuration"
            )
        # A stored config never chooses where the server connects: the endpoint
        # comes from the server configuration for that provider+model (or the
        # provider's default). Otherwise a row could aim the server's outbound
        # call at any host — SSRF with no egress boundary (10) yet to stop it.
        spec = ModelSpec(
            provider=provider_name,
            model=model_config.model,
            endpoint=self._endpoints.get((provider_name, model_config.model)),
            timeout_seconds=float(model_config.timeout_seconds),
            pricing=pricing or ModelPricing(),
            generation_policy=dict(model_config.generation_policy or {}),
        )
        primary = self._build(spec, model_config.secret_ref, requester)
        allowed = frozenset((row.enabled_tools or []) + (row.model_tools or []))
        return ResolvedModels(chain=(primary, *fallbacks), allowed_tool_ids=allowed)
