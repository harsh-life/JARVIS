"""The Agent Compiler — the only writer of a CompiledAgentSpec (docs/29 §9).

Pipeline (§9.1):

    AgentDraft ─validate─► classify ─► select (§8) ─► abilities→capabilities (§9.3)
      ─► envelope ceiling (§10.2) ─► memory policy (§16) ─► trigger (§17) ─► outputs
      ─► budget (§10.5) ─► bounds ─► immutable, hashed CompiledAgentSpec

Pure (import contract AF-C2): its caller supplies the owner's server-side
context — identity, graph, model policy, budget policy, agent count, runtime
bounds, the operator's egress predicate — and a trigger previewer. Nothing
authorization-relevant is ever taken from the draft or guessed: an ambiguous
template, a reminder without an exact schedule, a file ability without a
named sandbox, or a URL the operator has not allowed is a
`needs_clarification` with closed choices where possible (§9.4).

The spec holds a **ceiling** (§10.1). Nothing in it is ever read as a grant:
at run time the owner's live grants, activation, the mode and risk ceilings
and the engine still decide each operation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Literal, Mapping
from uuid import UUID

from pydantic import ValidationError

from server.agents import abilities
from server.agents.delegation import unattended_spec_refusal
from server.agents.hashing import spec_hash_of
from server.agents.registry import AgentRegistries
from server.agents.selector import SelectionInput, SelectionResult, model_calls_bound, select
from server.capabilities.registry import CAPABILITY_REGISTRY
from shared.schemas.agent import TaskMode
from shared.schemas.agent_factory import (
    AbilityName,
    AgentDraft,
    AgentTemplate,
    ClarificationQuestion,
    CompiledAgentSpec,
    CompiledTrigger,
    EnvelopeEntry,
    HydrationSpec,
    NotebookAccess,
    OutputKind,
    SourceKind,
    SpecBounds,
    SpecBudget,
    TriggerKind,
    risk_severity,
)

COMPILER_VERSION = 1
INSTRUCTIONS_TEMPLATE_VERSION = 1

# Capabilities whose grant-level scope narrows by a sandbox label.
_SANDBOX_CAPABILITIES = frozenset({"file.read", "file.write"})

TriggerPreview = Callable[[str, str, datetime], datetime]


@dataclass(frozen=True)
class OwnerContext:
    """Server-side facts about the owner, never from the draft."""

    owner_user_id: UUID
    graph_id: UUID | None
    owner_primary_model_ref: str
    budget_per_run_policy: float
    budget_per_month_policy: float
    active_agent_count: int
    max_agents_per_user: int
    runtime_max_seconds: float
    runtime_max_model_calls: int
    runtime_max_tool_calls: int
    # The operator's EgressPolicy as a predicate (docs/29 §9.3): whether a URL
    # source is already allowed. The compiler never widens it.
    url_allowed: Callable[[str], bool]


@dataclass(frozen=True)
class CompileTarget:
    """Which agent/version the spec is for — allocated by the service."""

    agent_id: UUID
    version: int
    created_at: datetime


@dataclass(frozen=True)
class CompileResult:
    kind: Literal["compiled", "needs_clarification", "rejected"]
    spec: CompiledAgentSpec | None = None
    template: AgentTemplate | None = None
    selection: SelectionResult | None = None
    questions: tuple[ClarificationQuestion, ...] = ()
    reason_codes: tuple[str, ...] = field(default=())


def parse_draft(raw: Any) -> tuple[AgentDraft | None, tuple[str, ...]]:
    """Validate a worker-written draft. On failure, the offending field
    *names* (the top-level key where one exists) — never their values, which
    may be injected text or a leaked credential (docs/29 §9.2)."""

    if not isinstance(raw, Mapping):
        return None, ("draft",)
    try:
        return AgentDraft.model_validate(dict(raw)), ()
    except ValidationError as exc:
        names = sorted({str(err["loc"][0]) if err.get("loc") else "draft" for err in exc.errors()})
        return None, tuple(names)


def _rejected(*codes: str, **kwargs: Any) -> CompileResult:
    return CompileResult(kind="rejected", reason_codes=tuple(codes), **kwargs)


def _clarify(questions: list[ClarificationQuestion], **kwargs: Any) -> CompileResult:
    return CompileResult(kind="needs_clarification", questions=tuple(questions),
                         reason_codes=tuple(q.code for q in questions), **kwargs)


def _trigger(draft: AgentDraft, now: datetime, preview: TriggerPreview
             ) -> tuple[CompiledTrigger | None, list[ClarificationQuestion]]:
    request = draft.trigger_request
    if request.kind is TriggerKind.ON_DEMAND:
        return CompiledTrigger(kind=TriggerKind.ON_DEMAND, timezone=request.timezone or "UTC"), []
    questions: list[ClarificationQuestion] = []
    if request.cron is None:
        questions.append(ClarificationQuestion(
            code="schedule_needed",
            prompt="At exactly what time should the reminder come? Give the minute, hour and days "
                   f"(the request said: {request.schedule_text or 'no time'}).",
        ))
    if request.timezone is None:
        questions.append(ClarificationQuestion(code="timezone_needed",
                                               prompt="In which time zone? (for example Asia/Kolkata)"))
    if questions:
        return None, questions
    try:
        next_fire = preview(request.cron, request.timezone, now)
    except ValueError:
        return None, [ClarificationQuestion(code="schedule_invalid",
                                            prompt="That schedule is not a valid cron expression. "
                                                   "What exact time should it use?")]
    return CompiledTrigger(kind=request.kind, cron=request.cron, timezone=request.timezone,
                           next_fire_preview=next_fire), []


def _envelope(template: AgentTemplate, draft: AgentDraft) -> tuple[tuple[EnvelopeEntry, ...] | None, list[str]]:
    """docs/29 §10.2: map(template.abilities ∩ requested) ∩ registry − never
    mappable. Returns `None` (with reason codes) when the table itself would
    reach past what may ever be mapped — the compiler refuses rather than
    dropping the entry, so a widened table is loud (M-AG5)."""

    requested = [a for a in draft.requested_abilities if a in set(template.abilities)]
    labels = sorted({s.value for s in draft.sources if s.kind is SourceKind.SANDBOX_PATH})
    operations: dict[str, set[str]] = {}
    for ability in requested:
        mapping = abilities.ABILITY_TABLE[ability]
        if mapping.capability is None:
            continue
        if abilities.never_mappable(mapping.capability):
            return None, ["never_mappable"]
        definition = CAPABILITY_REGISTRY.get(mapping.capability)
        if definition is None or not set(mapping.operations) <= set(definition.operations):
            return None, ["unregistered_capability"]
        excluded = set(mapping.operations) & abilities.EXCLUDED_OPERATIONS.get(mapping.capability, frozenset())
        if excluded:
            return None, ["excluded_operation"]
        tiers = [definition.operations[op] for op in mapping.operations]
        if any(risk_severity(t) > risk_severity(template.risk_ceiling) for t in tiers):
            return None, ["above_risk_ceiling"]
        operations.setdefault(mapping.capability, set()).update(mapping.operations)

    entries: list[EnvelopeEntry] = []
    for capability in sorted(operations):
        ops = tuple(sorted(operations[capability]))
        if capability in _SANDBOX_CAPABILITIES:
            entries.extend(EnvelopeEntry(capability=capability, operations=ops, scope={"sandbox_root": label})
                           for label in labels)
        else:
            entries.append(EnvelopeEntry(capability=capability, operations=ops, scope={}))
    return tuple(entries), []


def compile_draft(
    draft: AgentDraft,
    *,
    owner: OwnerContext,
    target: CompileTarget,
    registries: AgentRegistries,
    runtime_health: Mapping[str, bool],
    trigger_preview: TriggerPreview,
    unattended_available: bool = False,
) -> CompileResult:
    # §9.6 rule 7: the owner's agent quota (a new agent only; an update is a
    # new version of an agent that already counts).
    if target.version == 1 and owner.active_agent_count >= owner.max_agents_per_user:
        return _rejected("agent_quota_reached")
    # §9.6 rule 6: v1 outputs are the owner's inbox (the schema allows nothing else).
    if draft.output_request is not OutputKind.INBOX:
        return _rejected("output_unavailable")
    # §9.6 rule 4 / §15: unattended runs exist only where the operator has
    # switched them on (OD-AF-2; `agents.unattended_enabled`).
    unattended = draft.trigger_request.kind is TriggerKind.UNATTENDED
    if unattended and not unattended_available:
        return _rejected("unattended_unavailable")

    selection = select(SelectionInput(
        template_hint=draft.template_hint,
        task_tags=frozenset(draft.task_tags),
        requested_abilities=frozenset(draft.requested_abilities),
        model_preference=draft.model_preference,
        preferred_model_features=draft.preferred_model_features,
        trigger_kind=draft.trigger_request.kind,
        owner_primary_model_ref=owner.owner_primary_model_ref,
        templates=tuple(registries.enabled_templates.values()),
        runtimes=tuple(registries.runtimes.values()),
        model_profiles=tuple(registries.model_profiles.values()),
        open_to_all=registries.open_to_all,
        available_infrastructure=registries.available_infrastructure,
        runtime_health=runtime_health,
        budget_per_run_policy=owner.budget_per_run_policy,
        budget_preference_per_run=draft.budget_preference_per_run,
        runtime_max_model_calls=owner.runtime_max_model_calls,
    ))
    if selection.kind == "needs_clarification":
        return _clarify(list(selection.questions), selection=selection)
    if selection.kind == "rejected":
        return _rejected(*selection.reason_codes, selection=selection)
    template = selection.template
    assert template is not None and selection.selection is not None
    if unattended and not template.unattended_supported:
        return _rejected("unattended_not_supported", template=template, selection=selection)

    envelope, codes = _envelope(template, draft)
    if envelope is None:
        return _rejected(*codes, template=template, selection=selection)

    questions: list[ClarificationQuestion] = []
    requested = set(draft.requested_abilities)
    if requested & {AbilityName.READ_SANDBOX_FILES, AbilityName.WRITE_SANDBOX_FILES} and not any(
        s.kind is SourceKind.SANDBOX_PATH for s in draft.sources
    ):
        questions.append(ClarificationQuestion(
            code="sandbox_needed", prompt="Which of your sandboxes should this agent work in? Name its label."))
    blocked = sorted({s.value for s in draft.sources if s.kind is SourceKind.URL and not owner.url_allowed(s.value)})
    if blocked:
        questions.append(ClarificationQuestion(
            code="source_not_allowlisted",
            prompt="These sites are not on the server's allowed list, so the agent could never read them. "
                   "Ask your server operator to allow them, or choose other sources: " + ", ".join(blocked),
        ))
    trigger, trigger_questions = _trigger(draft, target.created_at, trigger_preview)
    questions.extend(trigger_questions)
    if questions:
        return _clarify(questions, template=template, selection=selection)
    assert trigger is not None

    policy = template.memory_policy
    vault_sources = sorted({s.value for s in draft.sources if s.kind is SourceKind.VAULT_DOMAIN})
    if policy.vault_domains:
        vault_sources = [d for d in vault_sources if d in set(policy.vault_domains)]
    hydration = HydrationSpec(
        user_memory=AbilityName.READ_USER_MEMORY in requested and policy.user_memory_read,
        vault=AbilityName.READ_VAULT in requested and policy.vault_read,
        vault_domains=tuple(vault_sources) if AbilityName.READ_VAULT in requested else (),
    )
    notebook_enabled = policy.notebook is not NotebookAccess.NONE and bool(
        requested & {AbilityName.READ_AGENT_NOTEBOOK, AbilityName.WRITE_AGENT_NOTEBOOK})

    budget = SpecBudget(
        per_run=selection.budget_per_run,
        per_month=max(0.0, min(template.default_budget_per_month, owner.budget_per_month_policy)),
    )
    bounds = SpecBounds(
        max_run_seconds=max(1, min(template.max_run_seconds, math.floor(owner.runtime_max_seconds))),
        max_model_calls=model_calls_bound(template, owner.runtime_max_model_calls),
        max_tool_calls=max(0, min(template.max_tool_calls_per_run, owner.runtime_max_tool_calls)),
    )

    fields: dict[str, Any] = dict(
        agent_id=target.agent_id,
        version=target.version,
        owner_user_id=owner.owner_user_id,
        graph_id=owner.graph_id,
        name=draft.name,
        purpose=draft.purpose,
        desired_outcome=draft.desired_outcome,
        sources=draft.sources,
        selection=selection.selection,
        template_id=template.template_id,
        template_version=template.version,
        run_mode=template.run_mode,
        risk_ceiling=template.risk_ceiling,
        envelope_ceiling=envelope,
        hydration=hydration,
        notebook_enabled=notebook_enabled,
        trigger=trigger,
        outputs=(OutputKind.INBOX,),
        budget=budget,
        bounds=bounds,
        instructions_template_version=INSTRUCTIONS_TEMPLATE_VERSION,
        compiler_version=COMPILER_VERSION,
        created_at=target.created_at,
    )
    unsigned = CompiledAgentSpec.model_construct(**fields, spec_hash="0" * 64)
    spec = CompiledAgentSpec.model_validate(
        {**fields, "spec_hash": spec_hash_of(unsigned.model_dump(mode="json"))}
    )
    if unattended:
        # docs/29 §15.6–§15.7 (OD-AF-4): an unattended agent that could reach
        # anything above the unattended ceiling is refused, not narrowed.
        refused = unattended_spec_refusal(spec)
        if refused is not None:
            return _rejected(refused, template=template, selection=selection)
    return CompileResult(kind="compiled", spec=spec, template=template, selection=selection)


def verify_spec_hash(spec: CompiledAgentSpec) -> bool:
    """§9.6 rule 8 / AGENT-T33: the stored hash recomputes."""

    return spec_hash_of(spec.model_dump(mode="json")) == spec.spec_hash


def revalidation_required(spec: CompiledAgentSpec, templates: Mapping[str, AgentTemplate]) -> bool:
    """docs/29 §5.3: a spec built on an older version of its template (or on
    a template that no longer exists) must be recompiled and compared."""

    current = templates.get(spec.template_id)
    return current is None or spec.template_version < current.version


_MODE_ORDER = {TaskMode.OBSERVE: 0, TaskMode.SUGGEST: 0, TaskMode.DRAFT: 0, TaskMode.EXECUTE: 1}


def _entry_within(new: EnvelopeEntry, old_entries: tuple[EnvelopeEntry, ...]) -> bool:
    for old in old_entries:
        if old.capability != new.capability or not set(new.operations) <= set(old.operations):
            continue
        # A narrower scope keeps every key of the old one with the same value.
        if all(new.scope.get(k) == v for k, v in old.scope.items()):
            return True
    return False


def compare_specs(old: CompiledAgentSpec, new: CompiledAgentSpec) -> Literal["equal_or_narrower", "wider"]:
    """docs/29 §5.3 / §14.3: may a recompile be applied without the owner's
    re-approval? Only if nothing grew: envelope, mode, risk ceiling,
    hydration, notebook, budgets, bounds, trigger — and the same runtime and
    model profile (a different engine is never "narrower")."""

    narrower = (
        all(_entry_within(entry, old.envelope_ceiling) for entry in new.envelope_ceiling)
        and _MODE_ORDER[new.run_mode] <= _MODE_ORDER[old.run_mode]
        and risk_severity(new.risk_ceiling) <= risk_severity(old.risk_ceiling)
        and (not new.hydration.user_memory or old.hydration.user_memory)
        and (not new.hydration.vault or old.hydration.vault)
        and set(new.hydration.vault_domains) <= set(old.hydration.vault_domains)
        and (not new.notebook_enabled or old.notebook_enabled)
        and new.budget.per_run <= old.budget.per_run
        and new.budget.per_month <= old.budget.per_month
        and new.bounds.max_run_seconds <= old.bounds.max_run_seconds
        and new.bounds.max_model_calls <= old.bounds.max_model_calls
        and new.bounds.max_tool_calls <= old.bounds.max_tool_calls
        and new.trigger.kind is old.trigger.kind
        and new.trigger.cron == old.trigger.cron
        and new.trigger.timezone == old.trigger.timezone
        and set(new.outputs) <= set(old.outputs)
        and new.selection.runtime_id == old.selection.runtime_id
        and new.selection.model_profile_id == old.selection.model_profile_id
        and new.owner_user_id == old.owner_user_id
        and new.graph_id == old.graph_id
    )
    return "equal_or_narrower" if narrower else "wider"


__all__ = [
    "COMPILER_VERSION",
    "CompileResult",
    "CompileTarget",
    "INSTRUCTIONS_TEMPLATE_VERSION",
    "OwnerContext",
    "TriggerPreview",
    "compare_specs",
    "compile_draft",
    "parse_draft",
    "revalidation_required",
    "verify_spec_hash",
]
