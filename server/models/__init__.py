"""ModelProvider adapters — 06_MODEL_PROVIDER_LLM_TOOL.md.

`server/models` may resolve its own declared `secret_ref` via `server.secrets`
at the call boundary (12 §6's exception for tools/models, `pyproject.toml`'s
"Memory/vault never resolve secrets" contract comment) — nothing in this
package is reachable from `server.agent`, which is mechanically forbidden
from importing it at all (they are independent siblings under the layering
contract); the gateway composition root (`server/gateway/runtime.py`)
constructs a provider here and hands it to the agent runtime as a
`server.agent.ports.ModelInvoker`.

Importing this package registers every adapter it ships (currently: Ollama —
06 §2's local-first default) into `server.models.provider`'s registry.
"""

from server.models import ollama  # noqa: F401  (registers the Ollama adapter)
from server.models.provider import (
    ModelProvider,
    ProviderFactory,
    build_provider,
    register_adapter,
)

__all__ = ["ModelProvider", "ProviderFactory", "build_provider", "register_adapter"]
