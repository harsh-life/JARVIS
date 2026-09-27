"""OpenAI-compatible server STT/TTS (`/audio/transcriptions`, `/audio/speech`).

An explicit network component (docs/27 §1):

* **Declared egress.** Every request goes through `DeclaredOriginTransport`,
  which refuses anything not addressed to the configured endpoint's exact
  scheme, host and port; redirects are never followed. A provider cannot be
  steered to a second host — not by a response, not by configuration drift.
* **Key by reference.** `key_provider` is resolved per call (the composition
  root reads the `secret_ref`), placed in that one request's header, and
  dropped. It never reaches an exception, a result or a log line.
* **Bounded.** Audio in is bounded by the caller; audio out is read as a
  stream and cut off at `max_bytes`.
"""

from __future__ import annotations

import math
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from server.voice.audio import AudioBuffer
from server.voice.providers import SynthesizedAudio, Transcript, VoiceProviderUnavailable

KeyProvider = Callable[[], Awaitable[str]]

_DEFAULT_PORTS = {"https": 443, "http": 80}


class EgressRefused(httpx.TransportError):
    """A request to anything but the declared origin."""


class DeclaredOriginTransport(httpx.AsyncBaseTransport):
    def __init__(self, endpoint: str, inner: httpx.AsyncBaseTransport | None = None) -> None:
        parts = urlsplit(endpoint)
        self._origin = (parts.scheme, parts.hostname, parts.port or _DEFAULT_PORTS.get(parts.scheme))
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        origin = (url.scheme, url.host, url.port or _DEFAULT_PORTS.get(url.scheme))
        if origin != self._origin:
            raise EgressRefused("destination not declared for this voice provider")
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class _Client:
    def __init__(self, *, provider_id: str, endpoint: str, key_provider: KeyProvider | None,
                 transport: httpx.AsyncBaseTransport | None) -> None:
        self.provider_id = provider_id
        self._base = endpoint.rstrip("/")
        self._key_provider = key_provider
        self._transport = transport

    async def _headers(self) -> dict[str, str]:
        if self._key_provider is None:
            return {}
        try:
            key = await self._key_provider()
        except Exception:  # noqa: BLE001 — never say why a key failed to resolve
            raise VoiceProviderUnavailable("credential_unavailable") from None
        return {"Authorization": f"Bearer {key}"}

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=timeout, follow_redirects=False,
            transport=DeclaredOriginTransport(self._base, self._transport),
        )


class OpenAICompatibleSpeechToText(_Client):
    def __init__(self, *, provider_id: str, endpoint: str, model: str, key_provider: KeyProvider | None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(provider_id=provider_id, endpoint=endpoint, key_provider=key_provider,
                         transport=transport)
        self.model = model

    async def transcribe(self, audio: AudioBuffer, *, language: str | None, timeout: float) -> Transcript:
        headers = await self._headers()
        data = {"model": self.model, "response_format": "verbose_json"}
        if language:
            data["language"] = language
        try:
            async with self._client(timeout) as client:
                response = await client.post(
                    f"{self._base}/audio/transcriptions", headers=headers, data=data,
                    files={"file": (f"audio.{audio.extension}", audio.payload(), audio.media_type)},
                )
        except httpx.TimeoutException:
            raise VoiceProviderUnavailable("timeout") from None
        except EgressRefused:
            raise VoiceProviderUnavailable("egress_refused") from None
        except httpx.HTTPError as exc:
            raise VoiceProviderUnavailable(type(exc).__name__) from None
        finally:
            headers.clear()
        if response.status_code != 200:
            raise VoiceProviderUnavailable(f"http_{response.status_code}")
        try:
            body = response.json()
            text = body["text"]
            if not isinstance(text, str):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            raise VoiceProviderUnavailable("malformed_response") from None
        return Transcript(text=text, confidence=_confidence(body.get("segments")),
                          language=body.get("language") if isinstance(body.get("language"), str) else None)


class OpenAICompatibleTextToSpeech(_Client):
    MEDIA_TYPE = "audio/mpeg"

    def __init__(self, *, provider_id: str, endpoint: str, model: str, default_voice: str,
                 key_provider: KeyProvider | None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(provider_id=provider_id, endpoint=endpoint, key_provider=key_provider,
                         transport=transport)
        self.model = model
        self._voice = default_voice

    async def synthesize(self, text: str, *, voice: str | None, timeout: float, max_bytes: int) -> SynthesizedAudio:
        headers = await self._headers()
        payload = {"model": self.model, "input": text, "voice": voice or self._voice, "response_format": "mp3"}
        chunks = bytearray()
        try:
            async with self._client(timeout) as client:
                async with client.stream("POST", f"{self._base}/audio/speech", json=payload,
                                         headers=headers) as response:
                    if response.status_code != 200:
                        raise VoiceProviderUnavailable(f"http_{response.status_code}")
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > max_bytes:
                            raise VoiceProviderUnavailable("response_too_large")
        except httpx.TimeoutException:
            raise VoiceProviderUnavailable("timeout") from None
        except EgressRefused:
            raise VoiceProviderUnavailable("egress_refused") from None
        except httpx.HTTPError as exc:
            raise VoiceProviderUnavailable(type(exc).__name__) from None
        finally:
            headers.clear()
        if not chunks:
            raise VoiceProviderUnavailable("empty_audio")
        return SynthesizedAudio(audio=bytes(chunks), media_type=self.MEDIA_TYPE)


def _confidence(segments: object) -> float | None:
    """exp(mean avg_logprob) over the segments, when the provider reports them."""

    if not isinstance(segments, list) or not segments:
        return None
    values = [s.get("avg_logprob") for s in segments if isinstance(s, dict)]
    values = [v for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    if not values:
        return None
    return max(0.0, min(1.0, math.exp(sum(values) / len(values))))


__all__ = ["DeclaredOriginTransport", "OpenAICompatibleSpeechToText", "OpenAICompatibleTextToSpeech"]
