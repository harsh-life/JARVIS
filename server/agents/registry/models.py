"""The AgentModelProfile registry (docs/29 §6).

A profile names a routing role over a model entry the operator **already**
configured (`agent.primary`, `agent.fallback`, `models_as_tools.<id>`, 06).
Resolving a profile yields routing facts only — provider name, model name,
prices, cost class — never the entry's endpoint or `secret_ref`: those stay
in the server configuration and are used only when `server.models` builds the
adapter, with the key resolved through the existing SecretStore path (06 §1,
12 §2). One authenticated JARVIS identity, operator-held provider
credentials, reused through handles: an agent never sees either.
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.schemas.agent_factory import AgentModelProfile, CostClass

# Combined input+output price per 1k tokens at which a paid profile moves up a
# class (`[IMPL]`: docs/29 §6.2 derives the class from pricing but fixes no
# thresholds).
_LOW_MAX = 0.002
_MEDIUM_MAX = 0.02


@dataclass(frozen=True)
class ModelEntryFacts:
    """What the registry may know about a configured model entry."""

    provider: str
    model: str
    input_per_1k_tokens: float = 0.0
    output_per_1k_tokens: float = 0.0


@dataclass(frozen=True)
class ResolvedModelProfile:
    profile: AgentModelProfile
    provider: str
    model: str
    input_per_1k_tokens: float
    output_per_1k_tokens: float
    local: bool
    cost_class: CostClass

    @property
    def profile_id(self) -> str:
        return self.profile.profile_id

    def projected_cost(self, model_calls: int) -> float:
        """docs/29 §8.3 step 6: an upper bound for a run — every call at the
        profile's full context window, priced as input and output alike
        (`[IMPL]`: docs/29 does not say how a template's "max context" is
        split). Zero for a local model."""

        if self.local:
            return 0.0
        per_call = (self.profile.context_window / 1000.0) * (self.input_per_1k_tokens + self.output_per_1k_tokens)
        return model_calls * per_call


def derive_cost_class(facts: ModelEntryFacts, *, local: bool) -> CostClass:
    if local:
        return CostClass.LOCAL
    combined = facts.input_per_1k_tokens + facts.output_per_1k_tokens
    if combined <= _LOW_MAX:
        return CostClass.LOW
    if combined <= _MEDIUM_MAX:
        return CostClass.MEDIUM
    return CostClass.HIGH


__all__ = ["ModelEntryFacts", "ResolvedModelProfile", "derive_cost_class"]
