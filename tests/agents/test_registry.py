"""docs/29 §5–§7, §9.3, §24 — the registries, the ability table and the
`agents` configuration section. Every one is fail-closed: an invalid template,
profile, runtime toggle or ability mapping stops startup rather than loading
something narrower-than-intended or wider-than-reviewed."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from server.agents import abilities
from server.agents.registry import AgentRegistryError, ModelEntryFacts, load_templates
from server.agents.registry.runtimes import BROWSER_USE_IMAGE, NATIVE_RUNTIME
from server.agents.registry.templates import TEMPLATE_DIR
from server.capabilities.registry import CAPABILITY_REGISTRY
from server.config.schema import AgentsConfig, AppConfig
from shared.schemas.agent import TaskMode
from shared.schemas.agent_factory import AbilityName, CostClass
from shared.schemas.enums import RiskCategory
from tests.agents.support import ALL_TEMPLATES, REPOSITORY_TEMPLATES, entries, profile, registries

# ── the ability table (docs/29 §9.3) ─────────────────────────────────────


def test_the_shipped_ability_table_is_valid():
    abilities.validate_ability_table()


def test_every_ability_has_exactly_one_mapping():
    assert set(abilities.ABILITY_TABLE) == set(AbilityName)


@pytest.mark.parametrize("capability", [
    "agent.define", "agent.inspect", "agent.delete", "agent.run", "agent.anything",
    "system.restricted", "device.read", "device.ui_control", "app.interact",
    "superuser", "secret.read_raw", "capability.self_grant", "auth.disable",
])
def test_never_mappable(capability):
    assert abilities.never_mappable(capability)


def test_mappable_capabilities_are_registered_and_enumerated():
    for mapping in abilities.ABILITY_TABLE.values():
        if mapping.capability is None:
            assert mapping.runtime_owned is not None and not mapping.operations
            continue
        definition = CAPABILITY_REGISTRY[mapping.capability]
        assert set(mapping.operations) <= set(definition.operations)
        assert not abilities.never_mappable(mapping.capability)


def test_no_ability_ever_maps_to_a_delete():
    """docs/29 §9.3: `delete_file`/`bulk_delete` are excluded from every template in v1."""

    ops = {op for m in abilities.ABILITY_TABLE.values() for op in m.operations}
    assert not ops & {"delete_file", "bulk_delete", "post", "run_shell_command"}


def test_web_reading_is_get_only_with_no_scope_of_its_own():
    mapping = abilities.ABILITY_TABLE[AbilityName.READ_WEB_ALLOWLISTED]
    assert (mapping.capability, mapping.operations) == ("net.request", ("get",))


def test_the_model_tool_ability_asks_jarvis_for_a_call_it_never_holds_a_provider():
    mapping = abilities.ABILITY_TABLE[AbilityName.INVOKE_MODEL_TOOL]
    assert (mapping.capability, mapping.operations) == ("model.invoke", ("invoke",))


@pytest.mark.parametrize("capability,operations", [
    ("system.restricted", ("run_shell_command",)),
    ("agent.define", ("create",)),
    ("device.ui_control", ("tap",)),
    ("file.write", ("delete_file",)),
    ("net.request", ("post",)),
    ("file.read", ("format_disk",)),
    ("not.a.capability", ("x",)),
])
def test_agent_t32_a_widened_ability_table_fails_load(monkeypatch, capability, operations):
    """AGENT-T32 / M-AG5: an ability mapping that reaches a never-mappable
    capability, an excluded operation, or anything outside the registry stops
    the template registry from loading."""

    bad = dict(abilities.ABILITY_TABLE)
    bad[AbilityName.READ_WEB_ALLOWLISTED] = abilities.AbilityMapping(
        AbilityName.READ_WEB_ALLOWLISTED, capability, operations
    )
    monkeypatch.setattr(abilities, "ABILITY_TABLE", bad)
    with pytest.raises(AgentRegistryError):
        abilities.validate_ability_table()
    with pytest.raises(AgentRegistryError):
        load_templates()


# ── the shipped templates (docs/29 §5.5) ─────────────────────────────────


def test_the_four_v1_templates_load():
    templates = load_templates()
    assert set(templates) == set(REPOSITORY_TEMPLATES)
    # Phase 6: the browser template is execute/low_write, contained, never unattended.
    browser = templates["browser_monitor"]
    assert (browser.run_mode, browser.risk_ceiling, browser.unattended_supported) == (
        TaskMode.EXECUTE, RiskCategory.LOW_WRITE, False)
    assert browser.preferred_runtime == "browser_use" and browser.fallback_runtimes == ()
    assert templates["research_digest"].run_mode is TaskMode.OBSERVE
    assert templates["file_organizer"].run_mode is TaskMode.EXECUTE
    assert templates["file_organizer"].risk_ceiling is RiskCategory.LOW_WRITE
    assert not templates["file_organizer"].unattended_supported
    for t in templates.values():
        if t.run_mode is not TaskMode.EXECUTE:
            assert t.risk_ceiling is RiskCategory.LOW_READ


def test_the_pinned_browser_use_image_is_an_immutable_digest():
    """OD-AF-14: once a reviewed PR pins `BROWSER_USE_IMAGE`, it is never a
    floating tag — the exact registry path CI's `runtime-image.yml` builds
    and publishes, `@sha256:<64 hex>` and nothing else. The real launch-time
    validator (`ContainerSpec`) decides, so this fails the same way a run
    would, not by a second, drifting regex."""

    from server.execution.containers import ContainerSpec

    if BROWSER_USE_IMAGE is None:
        pytest.skip("not yet pinned (OD-AF-14): the runtime refuses to load")
    assert BROWSER_USE_IMAGE.startswith("ghcr.io/harsh-life/jarvis-browser-use@sha256:")
    assert re.fullmatch(r"ghcr\.io/harsh-life/jarvis-browser-use@sha256:[0-9a-f]{64}", BROWSER_USE_IMAGE)
    ContainerSpec(run_id=uuid.uuid4(), agent_id=uuid.uuid4(), image=BROWSER_USE_IMAGE,
                 socket_dir="/tmp/x/sockets", scratch_dir="/tmp/x/scratch")


def test_templates_live_in_the_repository_with_a_review_checklist():
    assert (TEMPLATE_DIR / "README.md").exists()
    assert sorted(p.stem for p in TEMPLATE_DIR.glob("*.yaml")) == sorted(REPOSITORY_TEMPLATES)


def _write(dirpath: Path, name: str, text: str) -> None:
    (dirpath / f"{name}.yaml").write_text(text)


def _copy_template(tmp_path: Path, name: str, **replace: str) -> Path:
    text = (TEMPLATE_DIR / f"{name}.yaml").read_text()
    for old, new in replace.items():
        assert old in text, old
        text = text.replace(old, new)
    _write(tmp_path, name, text)
    return tmp_path


@pytest.mark.parametrize("name,old,new", [
    # an observe template that could write
    ("research_digest", "abilities: [read_web_allowlisted, read_user_memory, read_vault]",
     "abilities: [read_web_allowlisted, read_user_memory, read_vault, write_sandbox_files]"),
    # a reminder is low_write: not in a read-only template
    ("knowledge_keeper", "abilities: [read_user_memory, read_vault, read_sandbox_files]",
     "abilities: [read_user_memory, read_vault, read_sandbox_files, create_reminder]"),
    # an ability the memory policy does not allow
    ("web_monitor_basic", "notebook: read_write", "notebook: read"),
    # authority smuggled into a template
    ("research_digest", "priority: 10", "priority: 10\ngrants: [system.restricted]"),
    ("research_digest", "template_id: research_digest", "template_id: something_else"),
    ("research_digest", "run_mode: observe", "run_mode: whenever"),
])
def test_an_invalid_template_stops_startup(tmp_path, name, old, new):
    with pytest.raises(AgentRegistryError):
        load_templates(_copy_template(tmp_path, name, **{old: new}))


def test_unparseable_or_empty_template_dirs_fail(tmp_path):
    _write(tmp_path, "broken", "template_id: [unclosed")
    with pytest.raises(AgentRegistryError):
        load_templates(tmp_path)


# ── registry assembly (docs/29 §24 load-time validation) ─────────────────


def test_default_registries_resolve_the_native_runtime_and_a_local_profile():
    reg = registries()
    assert set(reg.enabled_templates) == set(ALL_TEMPLATES)
    assert [r.runtime_id for r in reg.enabled_runtimes] == ["native"]
    [resolved] = reg.enabled_model_profiles
    assert resolved.cost_class is CostClass.LOCAL and resolved.local
    # A resolved profile carries routing facts only: no key, handle or endpoint.
    assert not {"secret_ref", "endpoint", "api_key"} & set(vars(resolved))


def test_disabled_features_load_nothing():
    reg = registries(enabled_templates=(), model_profiles=(), open_to_all=())
    assert reg.enabled_templates == {} and reg.enabled_model_profiles == ()


@pytest.mark.parametrize("kwargs,needle", [
    ({"enabled_templates": ("research_digest", "no_such_template")}, "no_such_template"),
    ({"runtimes": {"native": True, "letta": True}}, "letta"),
    ({"runtimes": {"native": True, "openhands": True}}, "openhands"),
    ({"runtimes": {"native": True, "my_runtime": False}}, "my_runtime"),
    ({"model_profiles": (profile(model_ref="agent.fallback"),)}, "agent.fallback"),
    ({"model_profiles": (profile(model_ref="models_as_tools.writer"),)}, "models_as_tools.writer"),
    ({"model_profiles": (profile(supported_runtimes=["my_runtime"]),)}, "my_runtime"),
    ({"model_profiles": (profile(),), "open_to_all": ("missing-profile",)}, "missing-profile"),
    ({"model_profiles": (profile(), profile())}, "general-agentic"),
])
def test_invalid_registry_configuration_fails_closed(kwargs, needle):
    with pytest.raises(AgentRegistryError) as exc:
        registries(**kwargs)
    assert needle in str(exc.value)


def test_a_profile_on_an_unimplemented_provider_fails_startup():
    with pytest.raises(AgentRegistryError) as exc:
        registries(model_entries=entries(**{"agent.primary": ModelEntryFacts("anthropic", "some-model")}))
    assert "anthropic" in str(exc.value)


def test_a_declared_cost_class_that_contradicts_pricing_fails():
    paid = entries(**{"agent.primary": ModelEntryFacts("openai", "gpt-x", 0.01, 0.03)})
    with pytest.raises(AgentRegistryError):
        registries(model_entries=paid, model_profiles=(profile(cost_class="local"),))
    reg = registries(model_entries=paid)
    assert reg.enabled_model_profiles[0].cost_class is CostClass.HIGH
    assert not reg.enabled_model_profiles[0].local


def test_a_disabled_native_runtime_leaves_no_runtime():
    assert registries(runtimes={"native": False}).enabled_runtimes == ()


def test_the_native_runtime_is_an_engine_not_an_authority():
    assert NATIVE_RUNTIME.human_approval_mode == "jarvis_gateway"
    assert NATIVE_RUNTIME.isolation_mode.value == "in_process"


# ── the `agents` configuration section (docs/29 §24) ─────────────────────


def _config(**agents) -> dict:
    return {
        "security": {"oidc": {"client_id": "c", "issuer": "https://accounts.google.com"}},
        "secrets": {"store": "encrypted_local", "kek_source": "env:KEK"},
        **({"agents": agents} if agents else {}),
    }


def test_the_feature_is_off_by_default():
    cfg = AppConfig.model_validate(_config())
    assert cfg.agents.enabled is False
    assert cfg.agents.enabled_templates == [] and cfg.agents.model_profiles == []
    assert cfg.agents.default_budget_per_run == 0.0 and cfg.agents.default_budget_per_month == 0.0
    assert cfg.agents.unattended_enabled is False


@pytest.mark.parametrize("agents", [
    {"unattended_enabled": True},
    {"unattended_enabled": True, "standing_delegation_ratified": True},   # the factory is off
    {"max_agents_per_user": 0},
    {"compile_preview_ttl_minutes": 0},
    {"default_budget_per_run": -1},
    {"enabled_templates": ["research_digest", "research_digest"]},
    {"model_profiles": [profile().model_dump(mode="json"), profile().model_dump(mode="json")]},
    {"model_profiles_open_to_all": ["nobody"]},
    {"runtimes": {"Bad Name": {"enabled": False}}},
    {"grants": ["file.write"]},
])
def test_invalid_agents_sections(agents):
    with pytest.raises(ValidationError):
        AppConfig.model_validate(_config(**agents))


def test_the_agents_section_is_its_own_model():
    assert AgentsConfig().runtimes["native"].enabled is True


# ── from the application configuration (server/composition/agents.py) ───


def test_registries_from_the_default_and_example_configuration():
    from server.composition.agents import registries_from_config
    from server.config.loader import load_config

    reg = registries_from_config(AppConfig.model_validate(_config()))
    assert reg.enabled_templates == {} and reg.enabled_model_profiles == ()
    example = load_config(Path(__file__).parents[2] / "config.example.yaml")
    assert registries_from_config(example).enabled_templates == {}


def test_profiles_reference_configured_entries_and_never_copy_their_credentials():
    from server.composition.agents import model_entry_facts, registries_from_config

    cfg = AppConfig.model_validate(_config(
        enabled_templates=["research_digest"],
        model_profiles=[
            profile().model_dump(mode="json"),
            profile("writer", model_ref="models_as_tools.writer", features=["writing", "structured_output"])
            .model_dump(mode="json"),
        ],
        model_profiles_open_to_all=["general-agentic", "writer"],
    ) | {"models_as_tools": [{
        "id": "writer", "provider": "openai", "model": "gpt-writer", "endpoint": "https://api.example.invalid/v1",
        "secret_ref": "secretstore:writer-key", "pricing": {"input_per_1k_tokens": 0.001, "output_per_1k_tokens": 0.002},
    }]})
    reg = registries_from_config(cfg)
    writer = reg.model_profiles["writer"]
    assert (writer.provider, writer.model, writer.local) == ("openai", "gpt-writer", False)
    facts = model_entry_facts(cfg)["models_as_tools.writer"]
    assert "secretstore:writer-key" not in repr(facts) and "api.example.invalid" not in repr(facts)
    assert "secretstore:writer-key" not in repr(writer) and "api.example.invalid" not in repr(writer)


def test_an_invalid_agents_section_stops_the_server_building():
    from server.composition import build_application
    from server.config.errors import ConfigError  # noqa: F401 — the loader's own failure type

    cfg = AppConfig.model_validate(_config(enabled_templates=["no_such_template"]))
    with pytest.raises(AgentRegistryError):
        build_application(cfg, extra_tools=())
