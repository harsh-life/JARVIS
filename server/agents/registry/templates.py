"""The AgentTemplate registry (docs/29 §5).

Templates are repository files (`server/agents/templates/<id>.yaml`),
code-reviewed like capability registry entries: adding or widening one is a
pull request, never a runtime action, and no endpoint or model can create,
edit or enable one (docs/29 §5.3). Loading is fail-closed: one invalid file
stops startup.

Beyond the schema, a template must be *internally consistent* with the
ability table and the mode/risk ceilings — a template is authority-limiting,
so a template whose abilities reach past its own ceiling is refused rather
than trusted to be narrowed later.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml
from pydantic import ValidationError

from server.agents import abilities
from server.agents.errors import AgentRegistryError
from shared.schemas.agent_factory import (
    UNATTENDED_RISK_CEILING,
    AbilityName,
    AgentTemplate,
    NotebookAccess,
    TriggerKind,
    risk_severity,
)

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

# Which memory-policy setting each runtime-owned ability needs (docs/29 §16.2).
_POLICY_NEEDS = {
    AbilityName.READ_USER_MEMORY: lambda p: p.user_memory_read,
    AbilityName.READ_VAULT: lambda p: p.vault_read,
    AbilityName.READ_AGENT_NOTEBOOK: lambda p: p.notebook in (NotebookAccess.READ, NotebookAccess.READ_WRITE),
    AbilityName.WRITE_AGENT_NOTEBOOK: lambda p: p.notebook is NotebookAccess.READ_WRITE,
}


def _check_consistency(template: AgentTemplate) -> None:
    ceiling = risk_severity(template.risk_ceiling)
    for ability in template.abilities:
        tier = abilities.ability_tier(ability)
        if tier is not None and risk_severity(tier) > ceiling:
            raise AgentRegistryError(
                f"template {template.template_id}: ability {ability.value} reaches {tier.value}, above the "
                f"template's risk_ceiling {template.risk_ceiling.value}"
            )
        needs = _POLICY_NEEDS.get(ability)
        if needs is not None and not needs(template.memory_policy):
            raise AgentRegistryError(
                f"template {template.template_id}: ability {ability.value} is not allowed by its memory_policy"
            )
    if len(set(template.abilities)) != len(template.abilities):
        raise AgentRegistryError(f"template {template.template_id}: duplicate abilities")
    # docs/29 §15.6–§15.7 (OD-AF-4): a template supports unattended runs only
    # if it says so in both places and its ceiling is within the unattended
    # one — never a template the compiler would have to narrow later.
    unattended = TriggerKind.UNATTENDED in template.trigger_support
    if unattended != template.unattended_supported:
        raise AgentRegistryError(
            f"template {template.template_id}: unattended_supported and trigger_support disagree")
    if unattended and risk_severity(template.risk_ceiling) > risk_severity(UNATTENDED_RISK_CEILING):
        raise AgentRegistryError(
            f"template {template.template_id}: unattended runs are capped at {UNATTENDED_RISK_CEILING.value}")


def parse_template(path: Path) -> AgentTemplate:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AgentRegistryError(f"{path.name}: unreadable template ({exc.__class__.__name__})") from None
    if not isinstance(raw, dict):
        raise AgentRegistryError(f"{path.name}: a template is a mapping")
    try:
        template = AgentTemplate.model_validate(raw)
    except ValidationError as exc:
        raise AgentRegistryError(f"{path.name}: invalid template: {exc}") from None
    if template.template_id != path.stem:
        raise AgentRegistryError(f"{path.name}: template_id {template.template_id!r} must match the file name")
    _check_consistency(template)
    return template


def load_templates(directory: Path | None = None) -> Mapping[str, AgentTemplate]:
    """Every template in the directory, validated, or `AgentRegistryError`."""

    abilities.validate_ability_table()
    directory = directory or TEMPLATE_DIR
    templates: dict[str, AgentTemplate] = {}
    for path in sorted(directory.glob("*.yaml")):
        template = parse_template(path)
        if template.template_id in templates:
            raise AgentRegistryError(f"duplicate template {template.template_id!r}")
        templates[template.template_id] = template
    return MappingProxyType(templates)


__all__ = ["TEMPLATE_DIR", "load_templates", "parse_template"]
