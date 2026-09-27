"""Fixtures for the voice suite (docs/27).

The production composition root (tests/runtime harness) with a scripted model.
A server voice provider is exercised against `ProviderStub`: an
`httpx.MockTransport` behind the real `DeclaredOriginTransport`, recording every
request that reached it — which is how a test proves where audio went, and
where it did not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx
import pytest

from tests.runtime.conftest import h, make_harness  # noqa: F401 — shared fixtures

KEY_ENV = "VOICE_TEST_PROVIDER_KEY"
KEY_VALUE = "voice-provider-key-not-real-7f3a"
ENDPOINT = "https://voice.test/v1"


def server_voice(**overrides) -> dict:
    provider = {
        "id": "cloud", "kind": "openai_compatible", "endpoint": ENDPOINT, "secret_ref": f"env:{KEY_ENV}",
        "stt_model": "whisper-1", "tts_model": "tts-1",
        "pricing": {"per_audio_mb": 0.01, "per_1k_chars": 0.015},
    }
    provider.update(overrides.pop("provider", {}))
    return {
        "voice": {"stt": "cloud", "tts": "cloud", "providers": [provider], **overrides},
        "security": {"budgets": {"per_user_daily_cost_limit": 5.0, "global_daily_cost_limit": 50.0}},
    }


@dataclass
class ProviderStub:
    transcript: str = "remind me to call the dentist tomorrow"
    speech: bytes = b"ID3\x04fake-mp3-bytes"
    status: int = 200
    requests: list[httpx.Request] = field(default_factory=list)
    bodies: list[bytes] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(request.read())
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "nope"}})
        if request.url.path.endswith("/audio/transcriptions"):
            return httpx.Response(200, json={
                "text": self.transcript, "language": "en",
                "segments": [{"avg_logprob": -0.1}, {"avg_logprob": -0.3}],
            })
        if request.url.path.endswith("/audio/speech"):
            payload = json.loads(request.read())
            assert payload["model"] == "tts-1"
            return httpx.Response(200, content=self.speech, headers={"content-type": "audio/mpeg"})
        return httpx.Response(404)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def provider(monkeypatch) -> ProviderStub:
    monkeypatch.setenv(KEY_ENV, KEY_VALUE)
    return ProviderStub()
