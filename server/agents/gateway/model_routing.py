"""Which configured model serves an agent's specialized model call
(docs/29 §12) — pure, deterministic, **not yet wired** (Phase 3).

An agent may use a model *as a tool* — a stronger writing model for a
writing subtask, an image-capable one for an image, a speech one for voice —
when its compiled envelope already holds `model.invoke`. The request
(`ModelCallRequest`) names only a role and a preference. This module:

1. refuses unless the spec's envelope contains `model.invoke.invoke` (the
   ability was requested, fit the template and was approved by the owner);
2. considers only operator-configured **model tools**
   (`models_as_tools.<id>` profiles) that are enabled, support the spec's
   runtime, are permitted for this owner (open to all, or the owner's own
   primary), serve the role, and fit the spec's per-run budget;
3. orders them by the preference, then id;
4. returns a `ModelRoute`: the model tool's id and the exact
   capability/operation/scope the call is then authorized as.

The route is data. Executing it is a `model.invoke` operation scoped to that
`model_tool_id`, decided by the owner's live grant, activation, the envelope
gate and the engine like any tool call — so routing can choose, never
authorize. The agent never sees a provider, a model name, an endpoint or a
key: the model tool resolves its own configured `secret_ref` inside
`server.models` (06 §1).
"""

from __future__ import annotations

from dataclasses import dataclass

from server.agents.registry import AgentRegistries
from server.agents.registry.models import ResolvedModelProfile
from shared.schemas.agent_factory import CompiledAgentSpec, CostClass, LatencyClass, ModelCallRequest, ModelPreference

MODEL_INVOKE = "model.invoke"
INVOKE = "invoke"
_MODEL_TOOL_PREFIX = "models_as_tools."

_LATENCY = {LatencyClass.LOW: 0, LatencyClass.MEDIUM: 1, LatencyClass.HIGH: 2}
_COST = {CostClass.LOCAL: 0, CostClass.LOW: 1, CostClass.MEDIUM: 2, CostClass.HIGH: 3}


@dataclass(frozen=True)
class ModelRoute:
    profile_id: str
    model_tool_id: str
    capability: str = MODEL_INVOKE
    operation: str = INVOKE

    @property
    def scope(self) -> dict[str, str]:
        return {"model_tool_id": self.model_tool_id}


@dataclass(frozen=True)
class RouteRefused:
    reason: str


def _order(request: ModelCallRequest):
    def key(m: ResolvedModelProfile):
        if request.preference is ModelPreference.FASTER:
            pref: tuple = (_LATENCY[m.profile.latency_class],)
        elif request.preference is ModelPreference.CHEAPER:
            pref = (_COST[m.cost_class],)
        elif request.preference is ModelPreference.THOROUGH:
            pref = (-m.profile.context_window, -len(m.profile.features))
        else:
            pref = ()
        return (*pref, m.profile_id)

    return key


def route_model_call(
    request: ModelCallRequest,
    *,
    spec: CompiledAgentSpec,
    registries: AgentRegistries,
    owner_primary_model_ref: str,
) -> ModelRoute | RouteRefused:
    invoke_entries = [e for e in spec.envelope_ceiling if e.capability == MODEL_INVOKE and INVOKE in e.operations]
    if not invoke_entries:
        return RouteRefused("model_invoke_not_in_envelope")
    pinned = {e.scope["model_tool_id"] for e in invoke_entries if "model_tool_id" in e.scope}

    candidates = []
    for m in registries.model_profiles.values():
        p = m.profile
        if not p.enabled or not p.model_ref.startswith(_MODEL_TOOL_PREFIX):
            continue
        tool_id = p.model_ref[len(_MODEL_TOOL_PREFIX):]
        if pinned and tool_id not in pinned:
            continue
        if spec.selection.runtime_id not in p.supported_runtimes:
            continue
        if p.profile_id not in registries.open_to_all and p.model_ref != owner_primary_model_ref:
            continue
        if request.role is not None and request.role not in p.features:
            continue
        if m.projected_cost(1) > spec.budget.per_run:
            continue
        candidates.append(m)
    if not candidates:
        return RouteRefused("no_model_for_role" if request.role is not None else "no_model")
    chosen = sorted(candidates, key=_order(request))[0]
    return ModelRoute(profile_id=chosen.profile_id, model_tool_id=chosen.profile.model_ref[len(_MODEL_TOOL_PREFIX):])


__all__ = ["INVOKE", "MODEL_INVOKE", "ModelRoute", "RouteRefused", "route_model_call"]
