"""docs/27 §1 (VOI-T5 half) — a server voice provider is an explicit network
component: declared egress, metered calls, key by reference, bounded I/O,
replaceable by configuration alone."""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from server.gateway.app import API_V1_PREFIX
from server.storage.models import AuditEvent, UsageEvent
from server.voice.audio import AudioBuffer
from server.voice.openai_compatible import DeclaredOriginTransport, EgressRefused, OpenAICompatibleSpeechToText
from server.voice.providers import VoiceProviderUnavailable
from tests.voice.conftest import KEY_ENV, KEY_VALUE, ProviderStub, server_voice

VOICE = f"{API_V1_PREFIX}/voice"
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + bytes(64)


def audio_headers(actor) -> dict:
    return {**actor.auth, "Content-Type": "audio/wav"}


def error(resp) -> dict:
    return resp.json()["error"]


# ── declared egress ──────────────────────────────────────────────────────


async def test_the_transport_refuses_any_undeclared_origin():
    seen: list[str] = []
    inner = httpx.MockTransport(lambda r: (seen.append(str(r.url)), httpx.Response(200))[1])
    transport = DeclaredOriginTransport("https://voice.test/v1", inner)
    async with httpx.AsyncClient(transport=transport) as client:
        assert (await client.get("https://voice.test/v1/x")).status_code == 200
        for url in ("https://evil.test/v1/x", "http://voice.test/v1/x", "https://voice.test:8443/v1/x",
                    "https://169.254.169.254/latest/meta-data"):
            with pytest.raises(EgressRefused):
                await client.get(url)
    assert seen == ["https://voice.test/v1/x"]


async def test_a_redirect_is_never_followed(make_harness, provider):
    """A provider that answers 302 to another host gets no second request —
    the audio goes to the declared endpoint and nowhere else."""

    followed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        followed.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://collector.evil.test/upload"})

    h = await make_harness(config=server_voice(), transport=httpx.MockTransport(handler))
    alice = await h.user("alice")
    resp = await h.client.post(f"{VOICE}/transcribe", content=WAV, headers=audio_headers(alice))
    assert resp.status_code == 503 and error(resp)["details"]["reason"] == "stt_provider:http_302"
    assert followed == ["voice.test"]


async def test_placing_voice_on_the_device_contacts_no_provider(make_harness, provider):
    """No unexpected network provider: with STT/TTS on the device, a configured
    provider entry is never contacted, whatever the client sends."""

    config = server_voice()
    config["voice"].update({"stt": "device", "tts": None})
    h = await make_harness(config=config, transport=provider.transport)
    alice = await h.user("alice")
    await h.client.post(f"{VOICE}/transcribe", content=WAV, headers=audio_headers(alice))
    await h.client.post(f"{VOICE}/synthesize", json={"text": "hi"}, headers=alice.auth)
    assert provider.requests == []


async def test_a_provider_is_replaceable_by_configuration_alone(make_harness, provider):
    config = server_voice()
    config["voice"]["providers"].append({
        "id": "onprem", "endpoint": "http://127.0.0.1:9100/v1", "stt_model": "whisper-large-v3",
    })
    config["voice"]["stt"] = "onprem"
    h = await make_harness(config=config, transport=provider.transport)
    alice = await h.user("alice")
    resp = await h.client.post(f"{VOICE}/transcribe", content=WAV, headers=audio_headers(alice))
    assert resp.status_code == 200
    [request] = provider.requests
    assert (request.url.host, request.url.port) == ("127.0.0.1", 9100)
    assert "authorization" not in request.headers  # this provider declares no key
    [usage] = await h.rows(UsageEvent)
    assert (usage.provider, usage.model, usage.estimated_cost) == ("voice:onprem", "whisper-large-v3", 0.0)


# ── the key ──────────────────────────────────────────────────────────────


async def test_the_key_travels_only_in_the_one_request_header(make_harness, provider, caplog):
    caplog.set_level(logging.DEBUG)
    h = await make_harness(config=server_voice(), transport=provider.transport)
    alice = await h.user("alice")
    resp = await h.client.post(f"{VOICE}/transcribe", content=WAV, headers=audio_headers(alice))
    assert resp.status_code == 200
    assert provider.requests[0].headers["authorization"] == f"Bearer {KEY_VALUE}"
    assert KEY_VALUE not in resp.text
    assert KEY_VALUE not in caplog.text
    for row in await h.rows(AuditEvent) + await h.rows(UsageEvent):
        assert KEY_VALUE not in repr(vars(row))


async def test_a_missing_key_fails_closed_without_calling_out(make_harness, provider, monkeypatch):
    monkeypatch.delenv(KEY_ENV)
    h = await make_harness(config=server_voice(), transport=provider.transport)
    alice = await h.user("alice")
    resp = await h.client.post(f"{VOICE}/transcribe", content=WAV, headers=audio_headers(alice))
    assert resp.status_code == 503 and error(resp)["details"]["reason"] == "stt_provider:credential_unavailable"
    assert provider.requests == []


# ── bounds, timeouts, concurrency ────────────────────────────────────────


async def test_a_provider_timeout_is_explicit_and_metered(make_harness, provider):
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    h = await make_harness(config=server_voice(), transport=httpx.MockTransport(slow))
    alice = await h.user("alice")
    resp = await h.client.post(f"{VOICE}/transcribe", content=WAV, headers=audio_headers(alice))
    assert resp.status_code == 503 and error(resp)["details"]["reason"] == "stt_provider:timeout"
    assert len(await h.rows(UsageEvent)) == 1


async def test_synthesized_audio_is_cut_off_at_the_bound(make_harness, provider):
    provider.speech = b"\xff" * 5000
    h = await make_harness(config=server_voice(max_tts_audio_bytes=1000), transport=provider.transport)
    alice = await h.user("alice")
    resp = await h.client.post(f"{VOICE}/synthesize", json={"text": "long answer"}, headers=alice.auth)
    assert resp.status_code == 503 and error(resp)["details"]["reason"] == "tts_provider:response_too_large"


async def test_malformed_provider_responses_are_refused(make_harness, provider):
    h = await make_harness(config=server_voice(),
                           transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"words": 1})))
    alice = await h.user("alice")
    resp = await h.client.post(f"{VOICE}/transcribe", content=WAV, headers=audio_headers(alice))
    assert resp.status_code == 503 and error(resp)["details"]["reason"] == "stt_provider:malformed_response"


async def test_voice_calls_count_against_the_rate_limit(make_harness, provider):
    config = server_voice()
    config["security"]["rate_limits"] = {"per_user_requests_per_minute": 2}
    h = await make_harness(config=config, transport=provider.transport)
    alice = await h.user("alice")
    for _ in range(2):
        assert (await h.client.post(f"{VOICE}/transcribe", content=WAV, headers=audio_headers(alice))).status_code == 200
    over = await h.client.post(f"{VOICE}/transcribe", content=WAV, headers=audio_headers(alice))
    assert over.status_code == 429 and error(over)["details"]["limit"] == "per_user_rate"
    assert len(provider.requests) == 2


async def test_concurrent_requests_are_each_metered_and_isolated(make_harness, provider):
    h = await make_harness(config=server_voice(), transport=provider.transport)
    users = [await h.user(f"user{i}") for i in range(4)]
    responses = await asyncio.gather(*(
        h.client.post(f"{VOICE}/transcribe", content=WAV + bytes([i]), headers=audio_headers(u))
        for i, u in enumerate(users)
    ))
    assert [r.status_code for r in responses] == [200] * 4
    usage = await h.rows(UsageEvent)
    assert sorted(u.user_id for u in usage) == sorted(u.user_id for u in users)


async def test_the_adapter_reports_an_empty_speech_response():
    stt = OpenAICompatibleSpeechToText(
        provider_id="p", endpoint="https://voice.test/v1", model="m", key_provider=None,
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    )
    audio = AudioBuffer(media_type="audio/wav", max_bytes=100)
    audio.append(b"abc")
    with pytest.raises(VoiceProviderUnavailable) as exc:
        await stt.transcribe(audio, language=None, timeout=1)
    assert exc.value.reason == "http_500"


def test_provider_stub_is_what_the_suite_uses():
    assert ProviderStub().transport is not None
