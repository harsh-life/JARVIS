"""Which inference endpoint answers for each catalogue id (docs/31 §2).

A snapshot names catalogue entries by id and records their provider, model and
price — never an endpoint or a key. To run live, the harness is given a
**provider map**: a local YAML file mapping each catalogue id to an endpoint
and, for a paid provider, the *name* of an environment variable holding its
key. The map must agree with the snapshot on provider and model, so a snapshot
cannot silently run against a different model.

The harness never reads the SecretStore. Without `live=True`, only scripted
providers are built, and nothing leaves the process.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import yaml
from pydantic import BaseModel, ConfigDict, Field

from server.models.factory import build_provider
from server.models.provider import ChatMessage, ModelPricing, ModelResult, ModelSpec, ModelUnavailable
from shared.schemas.intelligence import ExperimentSnapshot, ResolvedModel

from experiments.accounting import BudgetLedger, CallLedger, MeteredModel


class ProviderMapEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    endpoint: str | None = None
    # The NAME of an environment variable holding the key; never the key.
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")


class ProviderMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    providers: dict[str, ProviderMapEntry]

    @classmethod
    def load(cls, path: Path) -> "ProviderMap":
        return cls.model_validate(yaml.safe_load(path.read_text()) or {})


Responder = Callable[[Sequence[ChatMessage]], "str | ModelResult | Exception"]


class ScriptedProvider:
    """An offline `ModelProvider`. `responder` sees the messages and returns
    the reply text (or a `ModelResult`, or an exception to raise). Token
    counts are estimated (chars/4) and marked as such by the runner."""

    def __init__(self, spec: ModelSpec, responder: Responder) -> None:
        self.spec = spec
        self._responder = responder
        self.calls: list[list[ChatMessage]] = []

    async def invoke(self, messages: Sequence[ChatMessage], *, timeout: float) -> ModelResult:
        self.calls.append(list(messages))
        out = self._responder(messages)
        if isinstance(out, Exception):
            raise out
        if isinstance(out, ModelResult):
            return out
        prompt = sum(len(m.content) for m in messages) // 4 + 1
        return ModelResult(content=out, prompt_tokens=prompt, completion_tokens=len(out) // 4 + 1)

    async def health(self) -> bool:
        return True


def _pricing(model: ResolvedModel) -> ModelPricing:
    return ModelPricing(input_per_1k_tokens=model.input_per_1k_tokens, output_per_1k_tokens=model.output_per_1k_tokens)


def generation_policy(provider: str, max_tokens: int) -> dict:
    # Deterministic decoding for reproducible comparisons; the cap is what
    # the reservation assumed, in each provider's own vocabulary.
    if provider == "ollama":
        return {"temperature": 0, "num_predict": max_tokens}
    return {"temperature": 0, "max_tokens": max_tokens}


@dataclass
class ModelPool:
    """Builds a metered model per (catalogue id, output cap)."""

    snapshot: ExperimentSnapshot
    ledger: CallLedger
    budget: BudgetLedger
    provider_map: ProviderMap | None = None
    live: bool = False
    scripted: Callable[[ResolvedModel, int], ScriptedProvider] | None = None
    prompt_guard: Callable[[str], None] | None = None

    def get(self, catalog_id: str, *, max_tokens: int) -> MeteredModel:
        resolved = self.snapshot.models.get(catalog_id)
        if resolved is None:
            raise KeyError(f"{catalog_id!r} is not in the snapshot")
        if not self.live:
            if self.scripted is None:
                raise RuntimeError("offline run without a scripted provider")
            provider = self.scripted(resolved, max_tokens)
        else:
            provider = self._live(resolved, max_tokens)
        return MeteredModel(catalog_id=catalog_id, provider=provider, pricing=_pricing(resolved),
                            ledger=self.ledger, budget=self.budget, prompt_guard=self.prompt_guard)

    def _live(self, resolved: ResolvedModel, max_tokens: int):
        if self.provider_map is None:
            raise RuntimeError("a live run needs a provider map")
        entry = self.provider_map.providers.get(resolved.catalog_id)
        if entry is None:
            raise RuntimeError(f"the provider map has no entry for {resolved.catalog_id!r}")
        if (entry.provider, entry.model) != (resolved.provider, resolved.model):
            raise RuntimeError(
                f"provider map disagrees with the snapshot for {resolved.catalog_id!r}: "
                f"{entry.provider}/{entry.model} vs {resolved.provider}/{resolved.model}"
            )
        key_provider = None
        if entry.api_key_env:
            env = entry.api_key_env

            async def key_provider() -> str:
                value = os.environ.get(env)
                if not value:
                    raise ModelUnavailable(f"{resolved.provider}: key not configured")
                return value

        spec = ModelSpec(provider=entry.provider, model=entry.model, endpoint=entry.endpoint,
                         timeout_seconds=resolved.timeout_seconds, pricing=_pricing(resolved),
                         generation_policy=generation_policy(entry.provider, max_tokens))
        return build_provider(spec, key_provider=key_provider)


def scripted_spec(resolved: ResolvedModel, max_tokens: int) -> ModelSpec:
    return ModelSpec(provider=resolved.provider, model=resolved.model, timeout_seconds=resolved.timeout_seconds,
                     pricing=_pricing(resolved), generation_policy=generation_policy(resolved.provider, max_tokens))


__all__ = ["ModelPool", "ProviderMap", "ProviderMapEntry", "ScriptedProvider", "generation_policy", "scripted_spec"]
