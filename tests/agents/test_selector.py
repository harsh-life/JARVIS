"""docs/29 §8 — deterministic selection of (template, runtime, model profile).

The worker's preferences (template hint, model preference, preferred model
features) can only *choose among* what the filters already admitted; they
never admit anything (AGENT-T20, M-AG3, M-AG4)."""

from __future__ import annotations

import random
from dataclasses import replace
from pathlib import Path

import pytest

from server.agents.registry import ModelEntryFacts
from server.agents.registry.templates import TEMPLATE_DIR
from server.agents.selector import SelectionInput, select
from shared.schemas.agent_factory import (
    AbilityName,
    InfraRequirement,
    ModelFeature,
    ModelPreference,
    TaskTag,
    TriggerKind,
)
from tests.agents.support import ALL_TEMPLATES, entries, profile, registries


def inp(reg, **overrides) -> SelectionInput:
    base = dict(
        template_hint=None,
        task_tags=frozenset({TaskTag.SECURITY_RESEARCH, TaskTag.SUMMARIZATION}),
        requested_abilities=frozenset({AbilityName.READ_WEB_ALLOWLISTED}),
        model_preference=None,
        preferred_model_features=(),
        trigger_kind=TriggerKind.ON_DEMAND,
        owner_primary_model_ref="agent.primary",
        templates=tuple(reg.enabled_templates.values()),
        runtimes=tuple(reg.runtimes.values()),
        model_profiles=tuple(reg.model_profiles.values()),
        open_to_all=reg.open_to_all,
        available_infrastructure=reg.available_infrastructure,
        runtime_health={"native": True},
        budget_per_run_policy=0.0,
        budget_preference_per_run=None,
        runtime_max_model_calls=16,
    )
    base.update(overrides)
    return SelectionInput(**base)


def test_the_canonical_request_selects_research_digest_on_native():
    result = select(inp(registries()))
    assert result.kind == "selected", result.reason_codes
    s = result.selection
    assert (s.template_id, s.runtime_id, s.model_profile_id) == ("research_digest", "native", "general-agentic")
    assert s.selection_reason and all(isinstance(line, str) for line in s.selection_reason)


# ── AGENT-T20: only enabled, compatible, healthy candidates ──────────────


def test_a_template_not_enabled_is_never_selected():
    reg = registries(enabled_templates=("web_monitor_basic",))
    result = select(inp(reg))
    assert result.kind != "selected" or result.selection.template_id != "research_digest"


def test_a_disabled_runtime_is_never_selected():
    """M-AG3: a runtime profile's `enabled` flag is checked by the selector
    itself, not only by whoever built the list."""

    result = select(inp(registries(runtimes={"native": False})))
    assert result.kind == "rejected" and "no_runtime" in result.reason_codes


def test_an_unhealthy_runtime_is_never_selected():
    result = select(inp(registries(), runtime_health={"native": False}))
    assert result.kind == "rejected" and "no_runtime" in result.reason_codes


def test_a_runtime_below_the_templates_isolation_is_never_selected(tmp_path):
    """M-AG4: `min_isolation` is a hard filter."""

    text = (TEMPLATE_DIR / "research_digest.yaml").read_text().replace(
        "min_isolation: in_process", "min_isolation: container_netns")
    (tmp_path / "research_digest.yaml").write_text(text)
    reg = registries(enabled_templates=("research_digest",), template_dir=tmp_path)
    result = select(inp(reg))
    assert result.kind == "rejected" and "no_runtime" in result.reason_codes


def test_a_template_needing_missing_infrastructure_is_filtered(tmp_path):
    text = (TEMPLATE_DIR / "research_digest.yaml").read_text().replace(
        "required_infrastructure: [none]", "required_infrastructure: [container]")
    (tmp_path / "research_digest.yaml").write_text(text)
    reg = registries(enabled_templates=("research_digest",), template_dir=tmp_path)
    result = select(inp(reg))
    assert result.kind != "selected"
    assert not reg.available_infrastructure & {InfraRequirement.CONTAINER}


def test_requested_abilities_must_fit_inside_the_template():
    """An agent asking for writes cannot land in a read-only template."""

    result = select(inp(registries(), requested_abilities=frozenset({
        AbilityName.READ_WEB_ALLOWLISTED, AbilityName.WRITE_SANDBOX_FILES})))
    assert result.kind != "selected"


# ── preferences choose; they never admit ─────────────────────────────────


def test_a_hint_naming_a_rejected_template_admits_nothing():
    result = select(inp(registries(), template_hint="file_organizer"))
    assert result.kind == "selected" and result.selection.template_id == "research_digest"
    assert any("file_organizer" in line for line in result.selection.selection_reason)


def test_a_hint_naming_a_candidate_wins():
    reg = registries()
    result = select(inp(reg, task_tags=frozenset({TaskTag.SUMMARIZATION}),
                        requested_abilities=frozenset({AbilityName.READ_USER_MEMORY}),
                        template_hint="knowledge_keeper"))
    assert result.kind == "selected" and result.selection.template_id == "knowledge_keeper"


def test_no_enabled_template_is_no_template():
    result = select(inp(registries(enabled_templates=())))
    assert result.kind == "rejected" and "no_template" in result.reason_codes


def test_zero_candidates_offers_the_closest_templates():
    result = select(inp(registries(), task_tags=frozenset({TaskTag.DATA_ANALYSIS})))
    assert result.kind == "needs_clarification" and "no_matching_template" in result.reason_codes
    assert 1 <= len(result.questions[0].choices) <= 3


def _write_twin_templates(tmp_path: Path) -> None:
    base = (TEMPLATE_DIR / "research_digest.yaml").read_text()
    (tmp_path / "digest_a.yaml").write_text(base.replace("template_id: research_digest", "template_id: digest_a"))
    (tmp_path / "digest_b.yaml").write_text(
        base.replace("template_id: research_digest", "template_id: digest_b")
        .replace("abilities: [read_web_allowlisted, read_user_memory, read_vault]",
                 "abilities: [read_web_allowlisted, read_agent_notebook, write_agent_notebook]")
        .replace("notebook: none", "notebook: read_write")
    )


def test_ambiguous_templates_ask_rather_than_guess(tmp_path):
    """docs/29 §8.3 step 4: a tie on tags and surface between templates that
    differ in abilities is a clarification with both descriptions."""

    _write_twin_templates(tmp_path)
    reg = registries(enabled_templates=("digest_a", "digest_b"), template_dir=tmp_path)
    result = select(inp(reg))
    assert result.kind == "needs_clarification" and "ambiguous_template" in result.reason_codes
    assert set(result.questions[0].choices) == {"digest_a", "digest_b"}
    assert select(inp(reg, template_hint="digest_b")).selection.template_id == "digest_b"


# ── models: permitted, capable, affordable ───────────────────────────────


PAID = ModelEntryFacts("openai", "gpt-paid", 0.00001, 0.00002)


def _paid_registries(**kwargs):
    return registries(
        model_profiles=(profile(), profile("paid-writer", model_ref="models_as_tools.writer",
                                           features=["structured_output", "writing"])),
        open_to_all=("general-agentic", "paid-writer"),
        model_entries=entries(**{"models_as_tools.writer": PAID}),
        **kwargs,
    )


def test_a_zero_budget_never_selects_a_paid_model():
    result = select(inp(_paid_registries(), preferred_model_features=(ModelFeature.WRITING,)))
    assert result.selection.model_profile_id == "general-agentic"


def test_only_a_paid_model_with_zero_budget_is_no_model_budget():
    reg = registries(model_profiles=(profile("paid-writer", model_ref="models_as_tools.writer"),),
                     open_to_all=("paid-writer",), model_entries=entries(**{"models_as_tools.writer": PAID}))
    result = select(inp(reg))
    assert result.kind == "rejected" and "no_model:budget" in result.reason_codes


def test_a_preferred_feature_orders_within_what_the_budget_allows():
    result = select(inp(_paid_registries(), budget_per_run_policy=100.0,
                        preferred_model_features=(ModelFeature.WRITING,)))
    assert result.selection.model_profile_id == "paid-writer"


def test_cheaper_prefers_the_local_model():
    result = select(inp(_paid_registries(), budget_per_run_policy=100.0,
                        model_preference=ModelPreference.CHEAPER))
    assert result.selection.model_profile_id == "general-agentic"


def test_a_profile_not_permitted_for_the_owner_is_never_selected():
    """docs/29 §6.2: a profile is usable by an owner only when it is open to
    all or references the owner's own resolved primary."""

    reg = registries(open_to_all=())
    assert select(inp(reg)).selection.model_profile_id == "general-agentic"   # the owner's primary
    result = select(inp(reg, owner_primary_model_ref="user:ollama:private-model"))
    assert result.kind == "rejected" and "no_model" in result.reason_codes


def test_a_profile_missing_a_required_feature_is_never_selected():
    reg = registries(model_profiles=(profile(features=["agentic_reasoning"]),))
    result = select(inp(reg))
    assert result.kind == "rejected" and "no_model" in result.reason_codes


def test_a_disabled_profile_is_never_selected():
    reg = registries(model_profiles=(profile(enabled=False),))
    assert select(inp(reg)).kind == "rejected"


# ── the property: a pure function ────────────────────────────────────────


def test_agent_t20_selection_is_deterministic_whatever_the_input_order():
    reg = _paid_registries()
    rng = random.Random(29)
    tags = list(TaskTag)
    abilities = list(AbilityName)
    for _ in range(300):
        base = inp(
            reg,
            task_tags=frozenset(rng.sample(tags, rng.randint(1, 4))),
            requested_abilities=frozenset(rng.sample(abilities, rng.randint(0, 3))),
            model_preference=rng.choice([None, *ModelPreference]),
            preferred_model_features=tuple(rng.sample(list(ModelFeature), rng.randint(0, 2))),
            template_hint=rng.choice([None, *ALL_TEMPLATES]),
            budget_per_run_policy=rng.choice([0.0, 100.0]),
        )
        first = select(base)
        shuffled = replace(
            base,
            templates=tuple(rng.sample(base.templates, len(base.templates))),
            model_profiles=tuple(rng.sample(base.model_profiles, len(base.model_profiles))),
        )
        again = select(shuffled)
        assert (first.kind, first.selection, first.reason_codes) == (again.kind, again.selection, again.reason_codes)
        if first.kind == "selected":
            t = reg.enabled_templates[first.selection.template_id]
            assert base.requested_abilities <= set(t.abilities)
            assert base.task_tags & set(t.task_tags)
            assert reg.runtimes[first.selection.runtime_id].enabled


@pytest.mark.parametrize("preference", [None, *ModelPreference])
def test_model_choice_never_changes_the_template(preference):
    """Model/runtime choice cannot move a request to a wider template."""

    result = select(inp(_paid_registries(), model_preference=preference, budget_per_run_policy=100.0))
    assert result.selection.template_id == "research_digest"
