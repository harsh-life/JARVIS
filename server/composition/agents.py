"""The Agent Factory, joined to the configuration and the security layer
(docs/29 §3.1).

`server.agents` is pure and store-light by contract (AF-C1/AF-C2); this module
is where it meets the server configuration, the model factory and — in later
slices — the authorization engine. It adds no policy of its own.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from server.agents.registry import AgentRegistries, ModelEntryFacts, build_registries
from server.config.schema import LOCAL_MODEL_PROVIDERS, AppConfig
from server.graph.ports import ResourceDescriptor
from server.models.factory import IMPLEMENTED_PROVIDERS
from server.storage.models import AgentDefinitionRow
from shared.schemas.agent_factory import AgentStatus
from shared.schemas.authorization import ResourceType
from shared.schemas.enums import Visibility


def _facts(entry) -> ModelEntryFacts:
    pricing = entry.pricing
    return ModelEntryFacts(
        provider=entry.provider,
        model=entry.model,
        input_per_1k_tokens=pricing.input_per_1k_tokens if pricing is not None else 0.0,
        output_per_1k_tokens=pricing.output_per_1k_tokens if pricing is not None else 0.0,
    )


def model_entry_facts(config: AppConfig) -> dict[str, ModelEntryFacts]:
    """The model entries a profile may reference (docs/29 §6.2), as routing
    facts only: no endpoint and no `secret_ref` leaves the configuration here."""

    facts = {"agent.primary": _facts(config.agent)}
    if config.agent.fallback is not None:
        facts["agent.fallback"] = _facts(config.agent.fallback)
    for entry in config.models_as_tools:
        facts[f"models_as_tools.{entry.id}"] = _facts(entry)
    return facts


def registries_from_config(config: AppConfig) -> AgentRegistries:
    """Build and validate the registries; `AgentRegistryError` is fatal to
    startup whether or not `agents.enabled` (a latent error is found at the
    deployment that introduced it, not the one that switches the feature on)."""

    agents = config.agents
    return build_registries(
        enabled_templates=tuple(agents.enabled_templates),
        model_profiles=tuple(agents.model_profiles),
        open_to_all=tuple(agents.model_profiles_open_to_all),
        runtime_toggles={k: v.enabled for k, v in agents.runtimes.items()},
        model_entries=model_entry_facts(config),
        implemented_providers=IMPLEMENTED_PROVIDERS,
        local_providers=frozenset(LOCAL_MODEL_PROVIDERS),
    )


class AgentDefinitionLoader:
    """`ResourceLoader` for `agentdefinition` (docs/29 §4.1): the engine's
    projection of a definition to its authorization facts — owner, private
    visibility, graph scope — and nothing else. A deleted definition is not
    loadable, so every operation on it is `not_found`, for its owner too."""

    async def load(self, session: AsyncSession, resource_type: ResourceType,
                   resource_ref: str) -> ResourceDescriptor | None:
        if resource_type is not ResourceType.AGENTDEFINITION:
            return None
        try:
            agent_id = uuid.UUID(str(resource_ref))
        except (ValueError, TypeError):
            return None
        row = await session.get(AgentDefinitionRow, agent_id)
        if row is None or row.status == AgentStatus.DELETED.value:
            return None
        return ResourceDescriptor(
            resource_type=ResourceType.AGENTDEFINITION,
            resource_ref=str(row.agent_id),
            owner_user_id=row.owner_user_id,
            # v1 agents are private (the table's check constraint says so too).
            visibility=Visibility.PRIVATE,
            graph_id=row.graph_id,
            source_user_id=row.owner_user_id,
        )


__all__ = ["AgentDefinitionLoader", "model_entry_facts", "registries_from_config"]
