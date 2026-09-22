"""The Ollama adapter — 06 §2's local-first default primary.

`[LOCKED]` (06 §2): "The default and recommended primary is a local model via
Ollama... no per-worker cloud dependency, no per-call cost, data stays
local." This is the one concrete `ModelProvider` this branch ships; other
providers register their own adapter the same way (`register_adapter`) —
"adding one is an adapter + config, not a runtime change" (MODEL-002).

Keyless by design (06 §1: "Keyless local (ollama) has `secret_ref = null`"),
so this adapter never touches `server.secrets` — there is nothing for it to
resolve.
"""

from __future__ import annotations

import httpx

from server.models.provider import register_adapter
from shared.schemas.enums import ModelProvider as ModelProviderName
from shared.schemas.runtime import (
    GenerationPolicy,
    ModelMessage,
    ModelResult,
    ModelUnavailable,
)

DEFAULT_ENDPOINT = "http://127.0.0.1:11434"


class OllamaProvider:
    """Structurally satisfies `server.models.provider.ModelProvider`."""

    def __init__(self, *, model: str, endpoint: str | None = None) -> None:
        self._model = model
        self._endpoint = (endpoint or DEFAULT_ENDPOINT).rstrip("/")

    async def invoke(
        self, messages: list[ModelMessage], policy: GenerationPolicy, timeout: float
    ) -> ModelResult:
        """06 §4: "a failing/misbehaving adapter fails *that call only*" —
        every failure mode below (timeout, connection refused, malformed
        response) becomes `ModelUnavailable`, which `server/agent`'s loop
        turns into an explicit task failure (FAIL-CORE-002), never a
        fabricated answer.
        """

        payload = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "options": _options_from_policy(policy),
        }
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(f"{self._endpoint}/api/chat", json=payload)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelUnavailable(f"ollama call failed: {exc}") from exc

        message = body.get("message") or {}
        content = message.get("content")
        if not isinstance(content, str):
            raise ModelUnavailable("ollama response had no message.content")

        return ModelResult(
            content=content,
            tokens_used=int(body.get("eval_count", 0) or 0),
            finish_reason="stop" if body.get("done", True) else "length",
        )

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self._endpoint}/api/tags")
            return response.status_code == 200
        except httpx.HTTPError:
            return False


def _options_from_policy(policy: GenerationPolicy) -> dict:
    options: dict[str, float | int | list[str]] = {}
    if policy.temperature is not None:
        options["temperature"] = policy.temperature
    if policy.max_tokens is not None:
        options["num_predict"] = policy.max_tokens
    if policy.top_p is not None:
        options["top_p"] = policy.top_p
    if policy.stop:
        options["stop"] = policy.stop
    return options


def _factory(model: str, endpoint: str | None, api_key: str | None) -> OllamaProvider:
    # `api_key` is accepted only to match `ProviderFactory`'s uniform shape
    # across adapters; Ollama is keyless (06 §1) and this adapter never uses
    # it for anything — passing one is silently ignored rather than an error,
    # since a config that sets a harmless-but-unused secret_ref should not
    # break startup.
    return OllamaProvider(model=model, endpoint=endpoint)


register_adapter(ModelProviderName.OLLAMA, _factory)

__all__ = ["OllamaProvider"]
