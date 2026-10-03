"""The Agent Factory's three registries, assembled and validated once at
startup (docs/29 §5–§7, §24).

`build_registries` takes plain values — the enabled template ids, the
operator's model profiles, the runtime toggles, the facts of the configured
model entries, the set of providers `server.models` implements — rather than
the application config, so this package stays pure (import contract AF-C2:
no store, runtime, tool, model or execution module). The composition root
extracts those values (`server/composition/agents.py`).

Every check here is load-time and fatal (docs/29 §24): an unknown template id,
a profile on an unimplemented provider or a missing model entry, a declared
cost class that contradicts pricing, a non-native runtime enabled without its
infrastructure. None of this is authority: a registry says what *could* be
selected; the owner's grants still decide what an agent may do.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from server.agents.errors import AgentRegistryError
from server.agents.registry.models import ModelEntryFacts, ResolvedModelProfile, derive_cost_class
from server.agents.registry.runtimes import REGISTERED_RUNTIMES, RESERVED_RUNTIME_IDS
from server.agents.registry.templates import load_templates
from shared.schemas.agent_factory import (
    AgentModelProfile,
    AgentRuntimeProfile,
    AgentTemplate,
    InfraRequirement,
)


@dataclass(frozen=True)
class AgentRegistries:
    templates: Mapping[str, AgentTemplate]
    enabled_templates: Mapping[str, AgentTemplate]
    runtimes: Mapping[str, AgentRuntimeProfile]
    model_profiles: Mapping[str, ResolvedModelProfile]
    open_to_all: frozenset[str]
    available_infrastructure: frozenset[InfraRequirement]

    @property
    def enabled_runtimes(self) -> tuple[AgentRuntimeProfile, ...]:
        return tuple(r for r in self.runtimes.values() if r.enabled)

    @property
    def enabled_model_profiles(self) -> tuple[ResolvedModelProfile, ...]:
        return tuple(p for p in self.model_profiles.values() if p.profile.enabled)


def _runtimes(toggles: Mapping[str, bool],
              infrastructure: frozenset[InfraRequirement] = frozenset({InfraRequirement.NONE}),
              ) -> dict[str, AgentRuntimeProfile]:
    for runtime_id, profile in REGISTERED_RUNTIMES.items():
        # Phase 6: an external runtime is enabled only with its infrastructure
        # (docs/29 §21) — never a weaker boundary than its profile names.
        available = infrastructure | {InfraRequirement.NONE}
        if toggles.get(runtime_id) and not set(profile.required_infrastructure) <= available:
            raise AgentRegistryError(
                f"agents.runtimes.{runtime_id}: needs {sorted(r.value for r in profile.required_infrastructure)}"
                " — containers, the HTTP Model Gateway and a pinned image (docs/29 §21)")
    for runtime_id, enabled in toggles.items():
        if runtime_id in REGISTERED_RUNTIMES:
            continue
        if runtime_id in RESERVED_RUNTIME_IDS:
            if enabled:
                raise AgentRegistryError(
                    f"agents.runtimes.{runtime_id}: no provider is registered for it — external runtimes need "
                    "container/netns isolation, an MCP transport and an egress proxy that do not exist in this "
                    "build (docs/29 §21, Phase 6)"
                )
            continue
        raise AgentRegistryError(f"agents.runtimes.{runtime_id}: unknown runtime")
    return {
        runtime_id: profile.model_copy(update={"enabled": bool(toggles.get(runtime_id, False))})
        for runtime_id, profile in REGISTERED_RUNTIMES.items()
    }


def _known_runtime(runtime_id: str) -> bool:
    return runtime_id in REGISTERED_RUNTIMES or runtime_id in RESERVED_RUNTIME_IDS


def _resolve_profile(
    profile: AgentModelProfile,
    entries: Mapping[str, ModelEntryFacts],
    implemented: frozenset[str],
    local_providers: frozenset[str],
) -> ResolvedModelProfile:
    where = f"agents.model_profiles[{profile.profile_id}]"
    facts = entries.get(profile.model_ref)
    if facts is None:
        raise AgentRegistryError(f"{where}: model_ref {profile.model_ref!r} names no configured model entry")
    if facts.provider not in implemented:
        raise AgentRegistryError(
            f"{where}: provider {facts.provider!r} is not implemented by server/models/factory.py"
        )
    unknown = [r for r in profile.supported_runtimes if not _known_runtime(r)]
    if unknown:
        raise AgentRegistryError(f"{where}: unknown runtimes {unknown}")
    local = facts.provider in local_providers
    derived = derive_cost_class(facts, local=local)
    if profile.cost_class is not None and profile.cost_class is not derived:
        raise AgentRegistryError(
            f"{where}: cost_class {profile.cost_class.value!r} contradicts the entry's pricing "
            f"({derived.value!r})"
        )
    return ResolvedModelProfile(
        profile=profile, provider=facts.provider, model=facts.model,
        input_per_1k_tokens=facts.input_per_1k_tokens, output_per_1k_tokens=facts.output_per_1k_tokens,
        local=local, cost_class=derived,
    )


def build_registries(
    *,
    enabled_templates: Sequence[str],
    model_profiles: Sequence[AgentModelProfile],
    open_to_all: Sequence[str],
    runtime_toggles: Mapping[str, bool],
    model_entries: Mapping[str, ModelEntryFacts],
    implemented_providers: frozenset[str],
    local_providers: frozenset[str],
    template_dir: Path | None = None,
    infrastructure: frozenset[InfraRequirement] = frozenset({InfraRequirement.NONE}),
) -> AgentRegistries:
    templates = load_templates(template_dir)

    enabled: dict[str, AgentTemplate] = {}
    for template_id in enabled_templates:
        if template_id not in templates:
            raise AgentRegistryError(f"agents.enabled_templates: unknown template {template_id!r}")
        if template_id in enabled:
            raise AgentRegistryError(f"agents.enabled_templates: {template_id!r} listed twice")
        enabled[template_id] = templates[template_id]

    runtimes = _runtimes(runtime_toggles, infrastructure)
    for template in templates.values():
        for runtime_id in (template.preferred_runtime, *template.fallback_runtimes):
            if not _known_runtime(runtime_id):
                raise AgentRegistryError(f"template {template.template_id}: unknown runtime {runtime_id!r}")

    resolved: dict[str, ResolvedModelProfile] = {}
    for profile in model_profiles:
        if profile.profile_id in resolved:
            raise AgentRegistryError(f"agents.model_profiles: {profile.profile_id!r} is defined twice")
        resolved[profile.profile_id] = _resolve_profile(profile, model_entries, implemented_providers,
                                                        local_providers)
    missing = [p for p in open_to_all if p not in resolved]
    if missing:
        raise AgentRegistryError(f"agents.model_profiles_open_to_all: unknown profiles {missing}")

    return AgentRegistries(
        templates=templates,
        enabled_templates=MappingProxyType(enabled),
        runtimes=MappingProxyType(runtimes),
        model_profiles=MappingProxyType(resolved),
        open_to_all=frozenset(open_to_all),
        # Phase 6: the container, netns and browser sandbox exist only when
        # the operator switched them on (composition root); no MCP server.
        available_infrastructure=frozenset({InfraRequirement.NONE}) | infrastructure,
    )


__all__ = [
    "AgentRegistries",
    "AgentRegistryError",
    "ModelEntryFacts",
    "ResolvedModelProfile",
    "build_registries",
    "load_templates",
]
