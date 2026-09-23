"""Ollama adapter — the default, local, keyless primary (06 §2, MODEL-001).

No `secret_ref`: a local model has no key, and this adapter accepts none.
"""

from __future__ import annotations

from typing import Sequence

import httpx

from server.models.provider import ChatMessage, ModelResult, ModelSpec, ModelUnavailable

DEFAULT_OLLAMA_ENDPOINT = "http://127.0.0.1:11434"


class OllamaProvider:
    def __init__(self, spec: ModelSpec, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.spec = spec
        self._base = (spec.endpoint or DEFAULT_OLLAMA_ENDPOINT).rstrip("/")
        self._transport = transport

    async def invoke(self, messages: Sequence[ChatMessage], *, timeout: float) -> ModelResult:
        payload = {
            "model": self.spec.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "options": dict(self.spec.generation_policy),
        }
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
                response = await client.post(f"{self._base}/api/chat", json=payload)
        except httpx.HTTPError as exc:
            raise ModelUnavailable(f"ollama: {type(exc).__name__}") from None

        if response.status_code != 200:
            raise ModelUnavailable(f"ollama: HTTP {response.status_code}")
        try:
            body = response.json()
            content = body["message"]["content"]
        except (ValueError, KeyError, TypeError):
            raise ModelUnavailable("ollama: malformed response") from None

        return ModelResult(
            content=str(content),
            prompt_tokens=int(body.get("prompt_eval_count") or 0),
            completion_tokens=int(body.get("eval_count") or 0),
        )

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0, transport=self._transport) as client:
                response = await client.get(f"{self._base}/api/tags")
        except httpx.HTTPError:
            return False
        return response.status_code == 200
