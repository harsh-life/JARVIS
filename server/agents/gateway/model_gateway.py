"""The Model Gateway's rules (docs/29 §12) — pure, deterministic, no store.

`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]` (docs/29). Every model call of an
agent run is a Model Gateway request: first the run context (its model token,
the run, the agent — `core.py`), then these two checks, then the existing
usage precheck, the configured provider, metering and attribution.

**Which model.** A request names an *alias*, never a model:

* `agent-model` — the run's own model: exactly the profile the owner approved
  in the spec (`selection.model_profile_id`, at that version);
* `model-tool:<id>` — a model reached as a tool (`agent.model` routing): the
  spec's envelope must hold `model.invoke` (pinned to that tool, if pinned),
  and an operator profile must reference that configured model tool.

Either way the profile must still be enabled, support the spec's runtime, be
permitted to the owner *now* (open to all, or the owner's resolved primary —
OD-RT-3, read live), and — when the caller says what it is about to call — be
exactly that configured provider and model. Nothing else is accepted: no
provider, endpoint, model name or key ever comes from a request, and a role or
a preference (routing metadata) is not an input here at all.

**How much.** The agent's month (docs/29 §10.5): a paid call is refused when
what the agent's runs have spent this UTC month — finished runs' totals plus
the live runs' attributed usage, so two concurrent runs cannot overspend it —
plus the call's projected cost would exceed the month's budget. A zero month
leaves only free (local) calls. The owner's budget (13) and the run's own
budget are the runtime's existing usage precheck, made just before this.

The gateway chooses nothing and grants nothing: a model tool call admitted
here has already passed the envelope gate, the owner's activation and grant,
and the engine on the ordinary tool path (Phase 2).
"""

from __future__ import annotations

from server.agents.registry import AgentRegistries
from server.agents.registry.models import ResolvedModelProfile
from shared.schemas.agent_factory import CompiledAgentSpec

AGENT_MODEL_ALIAS = "agent-model"
_MODEL_TOOL_ALIAS = "model-tool:"
_MODEL_TOOL_REF = "models_as_tools."
_MODEL_INVOKE, _INVOKE = "model.invoke", "invoke"


def model_tool_alias(tool_id: str) -> str:
    return f"{_MODEL_TOOL_ALIAS}{tool_id}"


def _model_tool_profile(registries: AgentRegistries, tool_id: str) -> ResolvedModelProfile | None:
    ref = f"{_MODEL_TOOL_REF}{tool_id}"
    return next((m for m in registries.model_profiles.values() if m.profile.model_ref == ref), None)


def model_refusal(
    spec: CompiledAgentSpec,
    registries: AgentRegistries,
    *,
    alias: str,
    provider: str | None,
    model: str | None,
    owner_primary_model_ref: str,
) -> str | None:
    """`None` if the run may call this model now; otherwise why not:
    `unknown_alias`, `model_invoke_not_in_envelope`, `profile_unavailable`,
    `not_permitted` or `model_mismatch`."""

    if alias == AGENT_MODEL_ALIAS:
        resolved = registries.model_profiles.get(spec.selection.model_profile_id)
        if resolved is None or resolved.profile.version != spec.selection.model_profile_version:
            return "profile_unavailable"
    elif alias.startswith(_MODEL_TOOL_ALIAS) and len(alias) > len(_MODEL_TOOL_ALIAS):
        tool_id = alias[len(_MODEL_TOOL_ALIAS):]
        invoke = [e for e in spec.envelope_ceiling if e.capability == _MODEL_INVOKE and _INVOKE in e.operations]
        pinned = {e.scope["model_tool_id"] for e in invoke if "model_tool_id" in e.scope}
        if not invoke or (pinned and tool_id not in pinned):
            return "model_invoke_not_in_envelope"
        resolved = _model_tool_profile(registries, tool_id)
        if resolved is None:
            return "profile_unavailable"
    else:
        return "unknown_alias"
    profile = resolved.profile
    if not profile.enabled or spec.selection.runtime_id not in profile.supported_runtimes:
        return "profile_unavailable"
    if profile.profile_id not in registries.open_to_all and profile.model_ref != owner_primary_model_ref:
        return "not_permitted"
    if (provider, model) != (None, None) and (provider, model) != (resolved.provider, resolved.model):
        return "model_mismatch"
    return None


def budget_refusal(*, projected_cost: float, month_spent: float, month_budget: float) -> str | None:
    """`None`, or `agent_budget_exhausted` when this paid call would take the
    agent's month past its budget."""

    if projected_cost <= 0:
        return None
    if month_spent + projected_cost > month_budget:
        return "agent_budget_exhausted"
    return None


__all__ = ["AGENT_MODEL_ALIAS", "budget_refusal", "model_refusal", "model_tool_alias"]
