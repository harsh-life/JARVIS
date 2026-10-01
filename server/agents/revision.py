"""A purpose-only revision of an agent (docs/29 §18) — pure, deterministic.

`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]` (docs/29). The Judge may suggest a
clearer wording of one agent's purpose (`agent.purpose`). Applying it is the
owner's ordinary, confirmed `agent.define.update`: a new draft compiled by the
one compiler, approved on its card. This module writes that draft — the
agent's own stored spec, re-described (its template, sources and trigger, the
abilities its envelope, hydration and notebook hold) with only the purpose
replaced — and names what such a revision may never change.

If recompiling the draft today would change anything in `authority_of` — the
envelope, the ceilings, the budget, the trigger, the runtime or the model —
the candidate is refused: a suggestion from the Judge never brings anything
but new words for the purpose (docs/29 §18).
"""

from __future__ import annotations

from typing import Any

from server.agents.abilities import ABILITY_TABLE
from server.agents.registry import AgentRegistries
from shared.schemas.agent_factory import AbilityName, AgentDraft, CompiledAgentSpec, TriggerRequest

_HYDRATION = {"user_memory_hydration": "user_memory", "vault_hydration": "vault"}
_NOTEBOOK = frozenset({"notebook_read", "notebook_write"})


def _abilities(spec: CompiledAgentSpec, registries: AgentRegistries) -> tuple[AbilityName, ...]:
    template = registries.templates.get(spec.template_id)
    allowed = set(template.abilities) if template is not None else set()
    found: list[AbilityName] = []
    for ability, mapping in ABILITY_TABLE.items():
        if mapping.capability is not None:
            held = any(e.capability == mapping.capability and set(mapping.operations) <= set(e.operations)
                       for e in spec.envelope_ceiling)
        elif mapping.runtime_owned in _HYDRATION:
            held = bool(getattr(spec.hydration, _HYDRATION[mapping.runtime_owned]))
        else:
            held = mapping.runtime_owned in _NOTEBOOK and spec.notebook_enabled and ability in allowed
        if held:
            found.append(ability)
    return tuple(found)


def redraft_with_purpose(spec: CompiledAgentSpec, purpose: str, registries: AgentRegistries) -> AgentDraft:
    """The draft that recompiles `spec` with `purpose` and nothing else new.
    Raises `pydantic.ValidationError` for a purpose no draft may carry."""

    template = registries.templates.get(spec.template_id)
    trigger = spec.trigger
    return AgentDraft(
        name=spec.name,
        purpose=purpose,
        desired_outcome=spec.desired_outcome,
        task_tags=tuple(template.task_tags[:5]) if template is not None else (),
        template_hint=spec.template_id,
        requested_abilities=_abilities(spec, registries),
        sources=spec.sources,
        trigger_request=TriggerRequest(kind=trigger.kind, cron=trigger.cron,
                                       timezone=trigger.timezone if trigger.cron else None),
        budget_preference_per_run=spec.budget.per_run,
    )


def authority_of(spec: CompiledAgentSpec) -> dict[str, Any]:
    """Everything a purpose candidate may never change (docs/29 §18)."""

    return {
        "owner": spec.owner_user_id,
        "graph": spec.graph_id,
        "template": spec.template_id,
        "run_mode": spec.run_mode,
        "risk_ceiling": spec.risk_ceiling,
        "envelope": spec.envelope_ceiling,
        "hydration": spec.hydration,
        "notebook": spec.notebook_enabled,
        "trigger": (spec.trigger.kind, spec.trigger.cron, spec.trigger.timezone),
        "outputs": spec.outputs,
        "budget": spec.budget,
        "bounds": spec.bounds,
        "sources": spec.sources,
        "runtime": spec.selection.runtime_id,
        "model": (spec.selection.model_profile_id, spec.selection.model_profile_version),
    }


__all__ = ["authority_of", "redraft_with_purpose"]
