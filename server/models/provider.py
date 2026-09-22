"""The ModelProvider interface — 06_MODEL_PROVIDER_LLM_TOOL.md §1.

`[LOCKED]` (MODEL-001): "The runtime speaks one normalized interface, never a
vendor SDK directly. Adapters implement it per provider." This module is that
interface plus the adapter registry; `server/models/ollama.py` is the one
concrete adapter this branch ships (06 §2: local-first Ollama is the default
and recommended primary). Adding another vendor is "an adapter + config, not
a runtime change" (MODEL-002) — this registry is exactly that extension
point, and a provider without a registered adapter fails explicitly
(`UnsupportedModelProvider`) rather than silently or by crash.
"""

from __future__ import annotations

from typing import Callable, Protocol

from shared.schemas.enums import ModelProvider as ModelProviderName
from shared.schemas.runtime import (
    GenerationPolicy,
    ModelMessage,
    ModelResult,
    UnsupportedModelProvider,
)


class ModelProvider(Protocol):
    """06 §1's normalized contract. Every adapter's `invoke` returns the same
    `ModelResult` shape regardless of vendor — provider-specific response
    fields are translated here, never passed upward (MODEL-002: "provider
    quirks never leak upward")."""

    async def invoke(
        self, messages: list[ModelMessage], policy: GenerationPolicy, timeout: float
    ) -> ModelResult: ...

    async def health(self) -> bool: ...


# 06 §1 (MODEL-002): "Adding one is an adapter + config, not a runtime
# change." Populated by `server/models/__init__.py`; a factory function takes
# `(model: str, endpoint: str | None, api_key: str | None)` and returns a
# constructed `ModelProvider`.
ProviderFactory = Callable[[str, "str | None", "str | None"], ModelProvider]

_ADAPTERS: dict[ModelProviderName, ProviderFactory] = {}


def register_adapter(provider: ModelProviderName, factory: ProviderFactory) -> None:
    _ADAPTERS[provider] = factory


def build_provider(
    provider: ModelProviderName, *, model: str, endpoint: str | None, api_key: str | None
) -> ModelProvider:
    """06 §1: resolve the adapter for `provider`, or fail explicitly.

    `[LOCKED]` (FAIL-CORE-002): an unsupported provider is never a fabricated
    call and never a silent fallback to some other provider — the caller (the
    gateway composition root) sees `UnsupportedModelProvider` and decides
    whether a configured fallback model applies (05 §5's deterministic
    fallback decision), which is this branch's job, not this factory's.
    """

    factory = _ADAPTERS.get(provider)
    if factory is None:
        raise UnsupportedModelProvider(
            f"no ModelProvider adapter is registered for {provider.value!r}"
        )
    return factory(model, endpoint, api_key)


__all__ = ["ModelProvider", "ProviderFactory", "build_provider", "register_adapter"]
