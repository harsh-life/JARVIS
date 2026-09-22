"""OpenAI-compatible chat-completions adapter (openai, deepseek, groq,
openai_compatible — 06 §1's provider enum).

The API key is obtained from `key_provider` **per call**, placed in the
`Authorization` header of that one request, and dropped. It never enters a
`ChatMessage`, a `ModelResult`, an exception message, or a log line
(SECRET-004, MP-T2) — every error below is built from a status code or an
exception *type*, never from a response body that could echo a header back.
"""

from __future__ import annotations

from typing import Sequence

import httpx

from server.models.provider import (
    ChatMessage,
    KeyProvider,
    ModelResult,
    ModelSpec,
    ModelUnavailable,
)

DEFAULT_ENDPOINTS = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "groq": "https://api.groq.com/openai/v1",
}


class OpenAICompatibleProvider:
    def __init__(
        self,
        spec: ModelSpec,
        *,
        key_provider: KeyProvider | None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        endpoint = spec.endpoint or DEFAULT_ENDPOINTS.get(spec.provider)
        if not endpoint:
            raise ValueError(f"provider {spec.provider!r} needs an explicit endpoint")
        self.spec = spec
        self._base = endpoint.rstrip("/")
        self._key_provider = key_provider
        self._transport = transport

    async def _headers(self) -> dict[str, str]:
        if self._key_provider is None:
            return {}
        try:
            key = await self._key_provider()
        except Exception:  # noqa: BLE001 — never surface why a key failed to resolve
            # FAIL-012: a locked or denying SecretStore fails the call closed; the
            # call is not attempted without its credential.
            raise ModelUnavailable(f"{self.spec.provider}: credential unavailable") from None
        return {"Authorization": f"Bearer {key}"}

    async def invoke(self, messages: Sequence[ChatMessage], *, timeout: float) -> ModelResult:
        headers = await self._headers()
        payload = {
            "model": self.spec.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            **dict(self.spec.generation_policy),
        }
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self._transport) as client:
                response = await client.post(
                    f"{self._base}/chat/completions", json=payload, headers=headers
                )
        except httpx.HTTPError as exc:
            raise ModelUnavailable(f"{self.spec.provider}: {type(exc).__name__}") from None
        finally:
            headers.clear()

        if response.status_code != 200:
            raise ModelUnavailable(f"{self.spec.provider}: HTTP {response.status_code}")
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            usage = body.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelUnavailable(f"{self.spec.provider}: malformed response") from None

        return ModelResult(
            content=str(content or ""),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )

    async def health(self) -> bool:
        return True
