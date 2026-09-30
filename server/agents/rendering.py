"""What the owner is shown — rendered by deterministic code from a compiled
spec, never from worker prose (docs/29 §2.2, docs/23 §5.4).

The card's `does` line is the *template's* reviewed description, not the
worker's paraphrase of the owner's goal; `can` is derived from the envelope,
hydration and notebook; `cannot` is a fixed list minus what the envelope
actually allows. The agent's name (worker-written, validated as printable
text) appears only as a label.
"""

from __future__ import annotations

from server.agents.registry import AgentRegistries
from shared.schemas.agent_factory import (
    AgentConfirmationCard,
    AgentTemplate,
    CompiledAgentSpec,
    CompiledAgentSpecView,
    TriggerKind,
)

_CAN = {
    ("net.request", "get"): "read the web from approved sites",
    ("file.read", "read_file"): "read files in your sandbox {scope}",
    ("file.write", "write_file"): "create and change files in your sandbox {scope}",
    ("scheduler.create", "create_reminder"): "set reminders for you",
    ("model.invoke", "invoke"): "ask JARVIS for help from another configured model",
}

# Fixed: things no v1 agent can do. A line is dropped only when the envelope
# genuinely allows it (only `write files`, for file_organizer).
_CANNOT = (
    ("write files", "file.write"),
    ("send messages", None),
    ("post data to the web", None),
    ("control devices", None),
    ("create agents", None),
    ("run shell commands", None),
    ("change your memory", None),
    ("reach sites your server operator has not allowed", None),
)


def _money(value: float) -> str:
    return f"${value:.2f}"


def can_lines(spec: CompiledAgentSpec) -> tuple[str, ...]:
    lines: list[str] = []
    for entry in spec.envelope_ceiling:
        for (capability, operation), text in _CAN.items():
            if entry.capability == capability and operation in entry.operations:
                label = entry.scope.get("sandbox_root")
                lines.append(text.format(scope=f"“{label}”" if label else "").strip())
    if spec.hydration.user_memory:
        lines.append("read your JARVIS memory")
    if spec.hydration.vault:
        lines.append("read your knowledge vault")
    if spec.notebook_enabled:
        lines.append("keep its own notes between runs")
    return tuple(dict.fromkeys(lines))


def cannot_lines(spec: CompiledAgentSpec) -> tuple[str, ...]:
    held = {e.capability for e in spec.envelope_ceiling}
    return tuple(text for text, capability in _CANNOT if capability is None or capability not in held)


def trigger_line(spec: CompiledAgentSpec) -> str:
    if spec.trigger.kind is TriggerKind.REMINDER:
        return f"when you ask, or when you tap its reminder ({spec.trigger.cron}, {spec.trigger.timezone})"
    return "when you ask"


def budget_line(spec: CompiledAgentSpec) -> str:
    if spec.budget.per_run == 0.0:
        return "local models only — no paid calls"
    return (f"≤ {_money(spec.budget.per_run)} per run · ≤ {_money(spec.budget.per_month)} per month "
            "(counts toward your budget)")


def engine_line(spec: CompiledAgentSpec, registries: AgentRegistries) -> str:
    runtime = registries.runtimes.get(spec.selection.runtime_id)
    model = registries.model_profiles.get(spec.selection.model_profile_id)
    runtime_name = (runtime.display_name if runtime and runtime.display_name else spec.selection.runtime_id)
    model_name = (model.profile.display_name if model and model.profile.display_name
                  else spec.selection.model_profile_id)
    return f"{runtime_name} · model profile “{model_name}”"


def _template(spec: CompiledAgentSpec, registries: AgentRegistries) -> AgentTemplate | None:
    return registries.templates.get(spec.template_id)


def render_card(spec: CompiledAgentSpec, registries: AgentRegistries) -> AgentConfirmationCard:
    template = _template(spec, registries)
    return AgentConfirmationCard(
        title=f'Create agent "{spec.name}" (v{spec.version})' if spec.version == 1
        else f'Update agent "{spec.name}" to v{spec.version}',
        does=template.description if template else spec.template_id,
        can=can_lines(spec),
        cannot=cannot_lines(spec),
        runs=trigger_line(spec),
        results_go="your JARVIS inbox only",
        budget=budget_line(spec),
        engine=engine_line(spec, registries),
    )


def spec_view(spec: CompiledAgentSpec, registries: AgentRegistries) -> CompiledAgentSpecView:
    template = _template(spec, registries)
    return CompiledAgentSpecView(
        agent_id=spec.agent_id,
        version=spec.version,
        spec_hash=spec.spec_hash,
        template_id=spec.template_id,
        template_description=template.description if template else "",
        card=render_card(spec, registries),
        selection_reason=spec.selection.selection_reason,
    )


__all__ = [
    "budget_line",
    "can_lines",
    "cannot_lines",
    "engine_line",
    "render_card",
    "spec_view",
    "trigger_line",
]
