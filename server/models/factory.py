"""Provider selection by configuration (MODEL-002, P4, MP-T1).

Changing `agent.provider` in config changes which adapter this returns — no
runtime code changes. A provider in `01` §1.2's enum with no adapter yet
(`anthropic`, `gemini`, `custom`) is refused explicitly rather than routed to a
look-alike: adding it is development (15 §1), not configuration.
"""

from __future__ import annotations

import httpx

from server.models.ollama import OllamaProvider
from server.models.openai_compatible import OpenAICompatibleProvider
from server.models.provider import KeyProvider, ModelProvider, ModelSpec

_OPENAI_COMPATIBLE = frozenset({"openai", "deepseek", "groq", "openai_compatible"})


class ProviderNotImplemented(Exception):
    """The configured provider has no adapter in this build."""


def build_provider(
    spec: ModelSpec,
    *,
    key_provider: KeyProvider | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ModelProvider:
    if spec.provider == "ollama":
        return OllamaProvider(spec, transport=transport)
    if spec.provider in _OPENAI_COMPATIBLE:
        return OpenAICompatibleProvider(spec, key_provider=key_provider, transport=transport)
    raise ProviderNotImplemented(
        f"model provider {spec.provider!r} has no adapter in this build (06 §1)"
    )
