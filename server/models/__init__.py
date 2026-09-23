"""Model providers — 06_MODEL_PROVIDER_LLM_TOOL.md.

The runtime speaks one normalized interface (`ModelProvider`), never a vendor
SDK (MODEL-001). Adding a provider is an adapter plus configuration, not a
runtime change (MODEL-002, P4).

This package resolves no secret itself and imports nothing from
`server.secrets`: an adapter that needs an API key is handed a `KeyProvider`
callable by the composition root, which resolves the configured `secret_ref`
through the SecretStore at call time (06 §1, 16 §3). The key is used for one
HTTP request and is never placed in a message, a result, an exception, or a log
line (SECRET-004, MP-T2).
"""

from server.models.factory import ProviderNotImplemented, build_provider
from server.models.provider import (
    ChatMessage,
    KeyProvider,
    ModelPricing,
    ModelProvider,
    ModelResult,
    ModelSpec,
    ModelUnavailable,
)

__all__ = [
    "ChatMessage",
    "KeyProvider",
    "ModelPricing",
    "ModelProvider",
    "ModelResult",
    "ModelSpec",
    "ModelUnavailable",
    "ProviderNotImplemented",
    "build_provider",
]
