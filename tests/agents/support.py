"""Shared builders for the Agent Factory suites (docs/29)."""

from __future__ import annotations

from typing import Any

from server.agents.registry import AgentRegistries, ModelEntryFacts, build_registries
from shared.schemas.agent_factory import AgentModelProfile

IMPLEMENTED = frozenset({"ollama", "openai", "deepseek", "groq", "openai_compatible"})
LOCAL = frozenset({"ollama"})

ALL_TEMPLATES = ("research_digest", "web_monitor_basic", "knowledge_keeper", "file_organizer")
# Phase 6 (OD-AF-6): in the repository, enabled only by an operator who also
# switched the Browser Use runtime and its infrastructure on.
PHASE6_TEMPLATES = ("browser_monitor",)
REPOSITORY_TEMPLATES = ALL_TEMPLATES + PHASE6_TEMPLATES


def profile(profile_id: str = "general-agentic", **overrides: Any) -> AgentModelProfile:
    payload: dict[str, Any] = {
        "profile_id": profile_id, "version": 1, "model_ref": "agent.primary",
        "features": ["agentic_reasoning", "tool_calling", "structured_output"],
        "context_window": 8192, "latency_class": "medium", "supported_runtimes": ["native"],
        "enabled": True,
    }
    payload.update(overrides)
    return AgentModelProfile.model_validate(payload)


def entries(**extra: ModelEntryFacts) -> dict[str, ModelEntryFacts]:
    base = {"agent.primary": ModelEntryFacts(provider="ollama", model="qwen2.5:3b-instruct")}
    base.update(extra)
    return base


def registries(
    *,
    enabled_templates=ALL_TEMPLATES,
    model_profiles=None,
    open_to_all=("general-agentic",),
    runtimes=None,
    model_entries=None,
    **kwargs: Any,
) -> AgentRegistries:
    return build_registries(
        enabled_templates=tuple(enabled_templates),
        model_profiles=tuple(model_profiles if model_profiles is not None else (profile(),)),
        open_to_all=tuple(open_to_all),
        runtime_toggles=runtimes if runtimes is not None else {"native": True},
        model_entries=model_entries if model_entries is not None else entries(),
        implemented_providers=IMPLEMENTED,
        local_providers=LOCAL,
        **kwargs,
    )
