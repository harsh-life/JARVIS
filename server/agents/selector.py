"""Deterministic selection of (template, runtime, model profile) — docs/29 §8.

A pure function: the same `SelectionInput` always yields the same result,
whatever order its candidate lists arrive in (AGENT-T20). Every branch is
recorded in the rule trace (`selection_reason`) and every filter that removed
a candidate in `constraints_applied`, so "why this engine?" always has an
answer (docs/29 §26).

What the worker's words can and cannot do here:
* `task_tags` and `requested_abilities` *filter* — a template must contain
  every requested ability and share a tag;
* `template_hint`, `model_preference` and `preferred_model_features` only
  *choose among* candidates the filters admitted. A hint naming a template the
  filters rejected is recorded and ignored (§8.3 step 2); a preference never
  makes a profile eligible that the owner's model policy, the template's
  required features or the budget excluded (§6.2).
None of this is authority: it decides which *ceiling* is compiled, and the
owner's grants still decide what any run may do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

from server.agents.registry.models import ResolvedModelProfile
from shared.schemas.agent_factory import (
    ISOLATION_ORDER,
    AbilityName,
    AgentRuntimeProfile,
    AgentSelection,
    AgentTemplate,
    ClarificationQuestion,
    CostClass,
    InfraRequirement,
    LatencyClass,
    ModelFeature,
    ModelPreference,
    TaskTag,
    TriggerKind,
)

_LATENCY = {LatencyClass.LOW: 0, LatencyClass.MEDIUM: 1, LatencyClass.HIGH: 2}
_COST = {CostClass.LOCAL: 0, CostClass.LOW: 1, CostClass.MEDIUM: 2, CostClass.HIGH: 3}


@dataclass(frozen=True)
class SelectionInput:
    template_hint: str | None
    task_tags: frozenset[TaskTag]
    requested_abilities: frozenset[AbilityName]
    model_preference: ModelPreference | None
    preferred_model_features: tuple[ModelFeature, ...]
    trigger_kind: TriggerKind
    # docs/29 §6.2: the owner's resolved primary as a model reference —
    # `agent.primary` when the server configuration applies, anything else
    # (a user's own configured model) matches no profile.
    owner_primary_model_ref: str
    templates: tuple[AgentTemplate, ...]           # the operator-enabled templates
    runtimes: tuple[AgentRuntimeProfile, ...]      # every registered runtime; `enabled` is checked here
    model_profiles: tuple[ResolvedModelProfile, ...]  # every profile; `enabled` is checked here
    open_to_all: frozenset[str]
    available_infrastructure: frozenset[InfraRequirement]
    runtime_health: Mapping[str, bool]
    budget_per_run_policy: float
    budget_preference_per_run: float | None
    runtime_max_model_calls: int


@dataclass(frozen=True)
class SelectionResult:
    kind: Literal["selected", "needs_clarification", "rejected"]
    selection: AgentSelection | None = None
    template: AgentTemplate | None = None
    runtime: AgentRuntimeProfile | None = None
    model: ResolvedModelProfile | None = None
    budget_per_run: float = 0.0
    questions: tuple[ClarificationQuestion, ...] = ()
    reason_codes: tuple[str, ...] = ()


def budget_per_run(template: AgentTemplate, policy: float, preference: float | None) -> float:
    """docs/29 §9.6 rule 5: the tighter of the template's default and the
    owner policy; the draft's preference may only lower it."""

    ceiling = min(template.default_budget_per_run, policy)
    if preference is not None:
        ceiling = min(ceiling, preference)
    return max(0.0, ceiling)


def model_calls_bound(template: AgentTemplate, runtime_max_model_calls: int) -> int:
    return max(1, min(template.max_model_calls_per_run, runtime_max_model_calls))


def _tags(tags) -> str:
    return "[" + ", ".join(sorted(t.value for t in tags)) + "]"


def _template_candidates(inp: SelectionInput, trace: list[str], constraints: list[str]) -> list[AgentTemplate]:
    candidates = []
    for template in sorted(inp.templates, key=lambda t: t.template_id):
        missing = inp.requested_abilities - set(template.abilities)
        if missing:
            constraints.append(f"template {template.template_id}: lacks abilities "
                               f"[{', '.join(sorted(a.value for a in missing))}]")
            continue
        overlap = inp.task_tags & set(template.task_tags)
        if not overlap:
            constraints.append(f"template {template.template_id}: no matching task tag")
            continue
        if inp.trigger_kind not in template.trigger_support:
            constraints.append(f"template {template.template_id}: does not support {inp.trigger_kind.value} runs")
            continue
        needs = set(template.required_infrastructure) - inp.available_infrastructure
        if needs:
            constraints.append(f"template {template.template_id}: needs unavailable infrastructure "
                               f"[{', '.join(sorted(n.value for n in needs))}]")
            continue
        candidates.append(template)
    return candidates


def _template_order(inp: SelectionInput):
    def key(t: AgentTemplate):
        return (-len(inp.task_tags & set(t.task_tags)), len(t.abilities), -t.priority, t.template_id)

    return key


def _choose_template(inp: SelectionInput, trace: list[str], constraints: list[str]) -> SelectionResult | AgentTemplate:
    if not inp.templates:
        return SelectionResult(kind="rejected", reason_codes=("no_template",))
    candidates = _template_candidates(inp, trace, constraints)
    if not candidates:
        closest = sorted(
            inp.templates,
            key=lambda t: (-len(inp.task_tags & set(t.task_tags)), -t.priority, t.template_id),
        )[:3]
        return SelectionResult(
            kind="needs_clarification",
            reason_codes=("no_matching_template",),
            questions=(ClarificationQuestion(
                code="no_matching_template",
                prompt="No agent type fits that request. Which of these is closest to what you want?",
                choices=tuple(t.template_id for t in closest),
            ),),
        )

    by_id = {t.template_id: t for t in candidates}
    if inp.template_hint is not None:
        if inp.template_hint in by_id:
            trace.append(f"template {inp.template_hint}: named by the request and eligible")
            return by_id[inp.template_hint]
        trace.append(f"template hint {inp.template_hint}: not eligible for this request; ignored")

    ordered = sorted(candidates, key=_template_order(inp))
    first = ordered[0]
    if len(ordered) > 1:
        second = ordered[1]
        key = _template_order(inp)
        tie = key(first)[:2] == key(second)[:2]
        differs = (set(first.abilities) != set(second.abilities) or first.run_mode is not second.run_mode
                   or first.risk_ceiling is not second.risk_ceiling)
        if tie and differs:
            return SelectionResult(
                kind="needs_clarification",
                reason_codes=("ambiguous_template",),
                questions=(ClarificationQuestion(
                    code="ambiguous_template",
                    prompt=f"Two agent types fit equally well. {first.template_id}: {first.description} "
                           f"{second.template_id}: {second.description} Which one?",
                    choices=(first.template_id, second.template_id),
                ),),
            )
    overlap = inp.task_tags & set(first.task_tags)
    trace.append(f"template {first.template_id}: matched tags {_tags(overlap)}, "
                 f"{len(first.abilities)} abilities, priority {first.priority}")
    return first


def _choose_runtime(inp: SelectionInput, template: AgentTemplate, trace: list[str],
                    constraints: list[str]) -> AgentRuntimeProfile | None:
    by_id = {r.runtime_id: r for r in inp.runtimes}
    matched_tags = inp.task_tags & set(template.task_tags)
    for runtime_id in (template.preferred_runtime, *template.fallback_runtimes):
        runtime = by_id.get(runtime_id)
        if runtime is None:
            constraints.append(f"runtime {runtime_id}: no provider registered")
            continue
        if not runtime.enabled:
            constraints.append(f"runtime {runtime_id}: not enabled by the operator")
            continue
        if not matched_tags <= runtime.supported_template_tags:
            constraints.append(f"runtime {runtime_id}: does not support tags {_tags(matched_tags)}")
            continue
        if ISOLATION_ORDER[runtime.isolation_mode] < ISOLATION_ORDER[template.min_isolation]:
            constraints.append(f"runtime {runtime_id}: isolation {runtime.isolation_mode.value} is below the "
                               f"template's {template.min_isolation.value}")
            continue
        if not set(runtime.required_infrastructure) <= inp.available_infrastructure:
            constraints.append(f"runtime {runtime_id}: needs unavailable infrastructure")
            continue
        if not set(template.required_model_features) <= runtime.supported_model_features:
            constraints.append(f"runtime {runtime_id}: cannot drive the template's required model features")
            continue
        if not inp.runtime_health.get(runtime_id, False):
            constraints.append(f"runtime {runtime_id}: unhealthy")
            continue
        role = "preferred" if runtime_id == template.preferred_runtime else "fallback"
        trace.append(f"runtime {runtime_id}: {role}, enabled, isolation {runtime.isolation_mode.value}, healthy")
        return runtime
    return None


def _model_order(inp: SelectionInput):
    preferred = set(inp.preferred_model_features)

    def key(m: ResolvedModelProfile):
        pref: tuple = ()
        if inp.model_preference is ModelPreference.FASTER:
            pref = (_LATENCY[m.profile.latency_class],)
        elif inp.model_preference is ModelPreference.CHEAPER:
            pref = (_COST[m.cost_class],)
        elif inp.model_preference is ModelPreference.THOROUGH:
            pref = (-m.profile.context_window, -len(m.profile.features))
        return (-len(preferred & m.profile.features), *pref, m.profile_id)

    return key


def _choose_model(inp: SelectionInput, template: AgentTemplate, runtime: AgentRuntimeProfile, ceiling: float,
                  trace: list[str], constraints: list[str]) -> tuple[ResolvedModelProfile | None, bool]:
    calls = model_calls_bound(template, inp.runtime_max_model_calls)
    eligible: list[ResolvedModelProfile] = []
    budget_only = False
    for m in sorted(inp.model_profiles, key=lambda p: p.profile_id):
        p = m.profile
        if not p.enabled:
            constraints.append(f"model {p.profile_id}: disabled")
            continue
        if not set(template.required_model_features) <= p.features:
            constraints.append(f"model {p.profile_id}: lacks required features")
            continue
        if runtime.runtime_id not in p.supported_runtimes:
            constraints.append(f"model {p.profile_id}: not supported on runtime {runtime.runtime_id}")
            continue
        if p.profile_id not in inp.open_to_all and p.model_ref != inp.owner_primary_model_ref:
            constraints.append(f"model {p.profile_id}: not permitted for this owner")
            continue
        cost = m.projected_cost(calls)
        if cost > ceiling:
            constraints.append(f"model {p.profile_id}: projected {cost:.4f} per run exceeds the budget {ceiling:.4f}")
            budget_only = True
            continue
        eligible.append(m)
    if not eligible:
        return None, budget_only
    chosen = sorted(eligible, key=_model_order(inp))[0]
    permission = "open to all" if chosen.profile_id in inp.open_to_all else "the owner's own model"
    trace.append(
        f"model {chosen.profile_id}: required features present, {permission}, "
        f"projected {chosen.projected_cost(calls):.4f} ≤ budget {ceiling:.4f}"
        + (f", preference {inp.model_preference.value}" if inp.model_preference else "")
    )
    return chosen, budget_only


def select(inp: SelectionInput) -> SelectionResult:
    trace: list[str] = []
    constraints: list[str] = []

    chosen = _choose_template(inp, trace, constraints)
    if isinstance(chosen, SelectionResult):
        return chosen
    template = chosen

    runtime = _choose_runtime(inp, template, trace, constraints)
    if runtime is None:
        return SelectionResult(kind="rejected", template=template, reason_codes=("no_runtime",))

    ceiling = budget_per_run(template, inp.budget_per_run_policy, inp.budget_preference_per_run)
    model, budget_only = _choose_model(inp, template, runtime, ceiling, trace, constraints)
    if model is None:
        codes = ("no_model", "no_model:budget") if budget_only else ("no_model",)
        return SelectionResult(kind="rejected", template=template, runtime=runtime, reason_codes=codes)

    return SelectionResult(
        kind="selected",
        selection=AgentSelection(
            template_id=template.template_id,
            template_version=template.version,
            runtime_id=runtime.runtime_id,
            runtime_version_pin=runtime.version_pin,
            model_profile_id=model.profile_id,
            model_profile_version=model.profile.version,
            selection_reason=tuple(trace),
            constraints_applied=tuple(constraints),
        ),
        template=template,
        runtime=runtime,
        model=model,
        budget_per_run=ceiling,
    )


__all__ = ["SelectionInput", "SelectionResult", "budget_per_run", "model_calls_bound", "select"]
