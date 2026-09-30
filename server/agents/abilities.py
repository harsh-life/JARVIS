"""Abilities → capabilities: the code-reviewed bridge from words to power
(docs/29 §9.3).

A worker names *abilities* (a closed enum). This table is the only place an
ability becomes something the authorization path knows: an exact capability
and an exact subset of its **enumerated** operations (07 §3), or a
runtime-owned function that is not a capability at all (memory and vault
hydration, the agent's own notebook — docs/29 §16).

What the table can never contain (docs/29 §9.3 "never mappable", checked at
load by `validate_ability_table`, AGENT-T32):

* `agent.*` — an agent never creates, updates, runs or deletes agents (no
  recursion, AGENT-T5);
* `system.restricted`, `device.*`, `app.interact` — no shell, no device or UI
  control by an agent in v1;
* any absolute-floor name (PERM-006);
* `file.write.delete_file` / `bulk_delete`, `net.request.post` — excluded
  from every v1 template.

Mapping an ability here does not let an agent use it. It only puts the
capability into the agent's *ceiling*; each call still needs the owner's live
grant, activation, the mode and risk ceilings, and the engine's decision.
`invoke_model_tool` maps to `model.invoke`: the agent may *ask JARVIS* for a
model call through the existing model-tool capability (06, MODELTOOL-001); it
never holds a provider, an endpoint or a key.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from server.agents.errors import AgentRegistryError
from server.capabilities.floor import floor_category_for_capability
from server.capabilities.registry import CAPABILITY_REGISTRY
from shared.schemas.agent_factory import AbilityName, risk_severity
from shared.schemas.enums import RiskCategory

# docs/29 §9.3 — refused by name, by namespace, and (for floor names) by the
# floor's own classifier.
NEVER_MAPPABLE_CAPABILITIES = frozenset({"system.restricted", "app.interact"})
NEVER_MAPPABLE_NAMESPACES = ("agent.", "device.")
# docs/29 §9.3: excluded from every template in v1 — irreversible deletes, and
# the network write / exfiltration surface (10 §6).
EXCLUDED_OPERATIONS: Mapping[str, frozenset[str]] = MappingProxyType({
    "file.write": frozenset({"delete_file", "bulk_delete"}),
    "net.request": frozenset({"post"}),
})


def never_mappable(capability: str) -> bool:
    normalized = capability.strip().lower()
    return (
        normalized in NEVER_MAPPABLE_CAPABILITIES
        or normalized.startswith(NEVER_MAPPABLE_NAMESPACES)
        or floor_category_for_capability(normalized) is not None
    )


@dataclass(frozen=True)
class AbilityMapping:
    ability: AbilityName
    capability: str | None
    operations: tuple[str, ...] = ()
    # For abilities that are not capabilities: which runtime-owned function
    # (docs/29 §16). `None` for capability-backed abilities.
    runtime_owned: str | None = None


def _table() -> Mapping[AbilityName, AbilityMapping]:
    rows = (
        # `net.request` has no grant-level scope keys: destinations are the
        # operator's EgressPolicy, which the agent cannot widen (docs/29 §9.3).
        AbilityMapping(AbilityName.READ_WEB_ALLOWLISTED, "net.request", ("get",)),
        AbilityMapping(AbilityName.READ_USER_MEMORY, None, runtime_owned="user_memory_hydration"),
        AbilityMapping(AbilityName.READ_VAULT, None, runtime_owned="vault_hydration"),
        AbilityMapping(AbilityName.READ_SANDBOX_FILES, "file.read", ("list_directory", "read_file", "stat")),
        AbilityMapping(AbilityName.WRITE_SANDBOX_FILES, "file.write", ("create_file", "write_file")),
        AbilityMapping(AbilityName.READ_AGENT_NOTEBOOK, None, runtime_owned="notebook_read"),
        AbilityMapping(AbilityName.WRITE_AGENT_NOTEBOOK, None, runtime_owned="notebook_write"),
        AbilityMapping(AbilityName.CREATE_REMINDER, "scheduler.create", ("create_reminder",)),
        AbilityMapping(AbilityName.INVOKE_MODEL_TOOL, "model.invoke", ("invoke",)),
    )
    return MappingProxyType({row.ability: row for row in rows})


ABILITY_TABLE: Mapping[AbilityName, AbilityMapping] = _table()


def validate_ability_table() -> None:
    """Fail closed on any mapping that could reach past the reviewed set
    (AGENT-T32, M-AG5). Called by every template load."""

    table = ABILITY_TABLE
    missing = set(AbilityName) - set(table)
    if missing:
        raise AgentRegistryError(f"abilities without a mapping: {sorted(a.value for a in missing)}")
    for ability, mapping in table.items():
        if mapping.ability is not ability:
            raise AgentRegistryError(f"{ability.value}: mapping is keyed under the wrong ability")
        if mapping.capability is None:
            if mapping.runtime_owned is None or mapping.operations:
                raise AgentRegistryError(f"{ability.value}: a non-capability ability names a runtime function only")
            continue
        capability = mapping.capability
        if never_mappable(capability):
            raise AgentRegistryError(f"{ability.value}: {capability!r} is never mappable to an agent (docs/29 §9.3)")
        definition = CAPABILITY_REGISTRY.get(capability)
        if definition is None:
            raise AgentRegistryError(f"{ability.value}: {capability!r} is not in the capability registry")
        if not mapping.operations:
            raise AgentRegistryError(f"{ability.value}: maps to no operation")
        unknown = set(mapping.operations) - set(definition.operations)
        if unknown:
            raise AgentRegistryError(
                f"{ability.value}: {sorted(unknown)} are not enumerated operations of {capability!r} (07 §3)"
            )
        excluded = set(mapping.operations) & EXCLUDED_OPERATIONS.get(capability, frozenset())
        if excluded:
            raise AgentRegistryError(f"{ability.value}: {sorted(excluded)} are excluded from every v1 template")


def ability_tier(ability: AbilityName) -> RiskCategory | None:
    """The highest registry tier any operation of the ability reaches, or
    `None` for a runtime-owned ability (which is not a capability, docs/29
    §5.5: the notebook write stays inside the agent's own state)."""

    mapping = ABILITY_TABLE[ability]
    if mapping.capability is None:
        return None
    definition = CAPABILITY_REGISTRY[mapping.capability]
    return max((definition.operations[op] for op in mapping.operations), key=risk_severity)


__all__ = [
    "ABILITY_TABLE",
    "AbilityMapping",
    "EXCLUDED_OPERATIONS",
    "NEVER_MAPPABLE_CAPABILITIES",
    "NEVER_MAPPABLE_NAMESPACES",
    "ability_tier",
    "never_mappable",
    "validate_ability_table",
]
