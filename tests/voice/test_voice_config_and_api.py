"""docs/27 §1/§4, 02 §10 — configuration, placement, and the voice endpoints.

VOI-T1 lives here: with every voice provider null, Track B is fully functional.
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from server.config.schema import AppConfig, VoiceConfig
from server.gateway.app import API_V1_PREFIX
from server.security.events import AuditAction
from server.storage.models import AuditEvent, ScheduledJob, UsageEvent
from shared.schemas.enums import UsageKind
from tests.runtime.conftest import final
from tests.voice.conftest import ENDPOINT, KEY_VALUE, server_voice


VOICE = f"{API_V1_PREFIX}/voice"
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + bytes(64)


def audio_headers(actor, media_type: str = "audio/wav") -> dict:
    return {**actor.auth, "Content-Type": media_type}


def error(resp) -> dict:
    return resp.json()["error"]


# ── configuration ────────────────────────────────────────────────────────


def test_the_default_placement_is_on_device():
    config = VoiceConfig()
    assert (config.stt, config.tts) == ("device", "device")
    assert config.diarization is None and config.speaker_id is None and config.providers == []


def test_every_voice_setting_may_be_null():
    config = VoiceConfig(stt=None, tts=None, diarization=None, speaker_id=None)
    assert config.stt is None and config.tts is None


@pytest.mark.parametrize("payload", [
    {"stt": "missing-provider"},
    {"tts": "cloud", "providers": [{"id": "cloud", "endpoint": "https://v.test", "stt_model": "w",
                                    "pricing": {}}]},
    {"diarization": "pyannote"},
    {"speaker_id": "voiceprint"},
    {"providers": [{"id": "device", "endpoint": "https://v.test", "stt_model": "w", "pricing": {}}]},
    {"providers": [{"id": "cloud", "endpoint": "http://v.test", "stt_model": "w", "pricing": {}}]},
    {"providers": [{"id": "cloud", "endpoint": "https://user:pw@v.test", "stt_model": "w", "pricing": {}}]},
    {"providers": [{"id": "cloud", "endpoint": "https://v.test", "stt_model": "w"}]},
    {"providers": [{"id": "cloud", "endpoint": "https://v.test", "pricing": {}}]},
    {"providers": [{"id": "cloud", "endpoint": "https://v.test", "stt_model": "w", "pricing": {},
                    "secret_ref": "sk-live-literal"}]},
    {"providers": [{"id": "a", "endpoint": "https://v.test", "stt_model": "w", "pricing": {}},
                   {"id": "a", "endpoint": "https://w.test", "stt_model": "w", "pricing": {}}]},
])
def test_invalid_voice_configuration_fails_at_load(payload):
    with pytest.raises(ValidationError):
        VoiceConfig.model_validate(payload)


def test_a_loopback_provider_needs_no_pricing():
    config = VoiceConfig.model_validate({"stt": "local", "providers": [
        {"id": "local", "endpoint": "http://127.0.0.1:9000/v1", "stt_model": "whisper"}]})
    assert config.stt == "local"


# ── placement and the API ────────────────────────────────────────────────


async def test_config_view_shows_placement_only(make_harness, provider):
    h = await make_harness(config=server_voice(tts="device"), transport=provider.transport)
    alice = await h.user("alice")
    resp = await h.client.get(f"{VOICE}/config", headers=alice.auth)
    assert resp.status_code == 200
    assert resp.json()["stt"] == "server" and resp.json()["tts"] == "device"
    text = resp.text
    assert "cloud" not in text and ENDPOINT not in text and "VOICE_TEST" not in text


async def test_device_placement_means_the_server_has_no_provider(h):
    """The default: STT and TTS on the phone. The server endpoints refuse
    cleanly and substitute nothing."""

    alice = await h.user("alice")
    view = (await h.client.get(f"{VOICE}/config", headers=alice.auth)).json()
    assert (view["stt"], view["tts"]) == ("device", "device")
    resp = await h.client.post(f"{VOICE}/transcribe", content=WAV, headers=audio_headers(alice))
    assert resp.status_code == 503
    assert error(resp)["details"] == {"dependency": "voice", "reason": "stt_device"}
    resp = await h.client.post(f"{VOICE}/synthesize", json={"text": "hello"}, headers=alice.auth)
    assert resp.status_code == 503 and error(resp)["details"]["reason"] == "tts_device"


async def test_voice_endpoints_need_authentication(h):
    assert (await h.client.get(f"{VOICE}/config")).status_code == 401
    assert (await h.client.post(f"{VOICE}/transcribe", content=WAV,
                                headers={"Content-Type": "audio/wav"})).status_code == 401
    assert (await h.client.post(f"{VOICE}/synthesize", json={"text": "hi"})).status_code == 401


async def test_voi_t1_with_every_provider_null_track_b_is_fully_functional(make_harness):
    """VOI-T1: voice is detachable. Tasks, reminders and the rest of the API
    run exactly as before; the voice endpoints answer an explicit 503."""

    h = await make_harness(config={"voice": {"stt": None, "tts": None, "diarization": None, "speaker_id": None}})
    alice = await h.user("alice")
    h.model.push(final("All done."))
    task = await h.submit(alice, "what is on my list today?")
    assert task.status_code == 200 and task.json()["status"] == "completed"
    job = await h.client.post(f"{API_V1_PREFIX}/jobs", json={"task_reason": "stretch", "schedule": "0 9 * * *"},
                              headers=alice.auth)
    assert job.status_code == 201
    assert len(await h.rows(ScheduledJob)) == 1

    view = (await h.client.get(f"{VOICE}/config", headers=alice.auth)).json()
    assert (view["stt"], view["tts"]) == ("off", "off")
    resp = await h.client.post(f"{VOICE}/transcribe", content=WAV, headers=audio_headers(alice))
    assert resp.status_code == 503 and error(resp)["details"]["reason"] == "stt_off"


def test_voi_t1_the_example_config_loads_and_every_null_is_accepted():
    from server.config.loader import load_config

    config = load_config("config.example.yaml")
    assert config.voice.stt == "device"
    payload = config.model_dump()
    payload["voice"] = {"stt": None, "tts": None, "diarization": None, "speaker_id": None}
    assert AppConfig.model_validate(payload).voice.stt is None


# ── server STT / TTS ─────────────────────────────────────────────────────


async def test_server_transcription_returns_ordinary_text(make_harness, provider):
    h = await make_harness(config=server_voice(), transport=provider.transport)
    alice = await h.user("alice")
    resp = await h.client.post(f"{VOICE}/transcribe", params={"language": "en"}, content=WAV,
                               headers=audio_headers(alice))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["transcript"] == "remind me to call the dentist tomorrow"
    assert body["audio_retained"] is False
    assert body["language"] == "en" and body["truncated"] is False
    assert math.isclose(body["confidence"], math.exp(-0.2), rel_tol=1e-6)
    assert set(body) == {"voice_event_id", "session_id", "transcript", "audio_retained", "timestamp",
                         "confidence", "language", "truncated"}

    [request] = provider.requests
    assert request.url.host == "voice.test" and request.url.path == "/v1/audio/transcriptions"
    assert request.headers["authorization"] == f"Bearer {KEY_VALUE}"
    assert WAV in provider.bodies[0]
    [usage] = await h.rows(UsageEvent)
    assert (usage.kind, usage.provider, usage.model) == (UsageKind.MODEL_CALL, "voice:cloud", "whisper-1")
    assert usage.estimated_cost > 0
    [event] = await h.rows(AuditEvent, AuditEvent.action == AuditAction.VOICE_TRANSCRIBED.value)
    assert event.resource == f"voice_event:{body['voice_event_id']}"


async def test_server_synthesis_returns_bounded_audio(make_harness, provider):
    h = await make_harness(config=server_voice(), transport=provider.transport)
    alice = await h.user("alice")
    resp = await h.client.post(f"{VOICE}/synthesize", json={"text": "Your task is done.", "voice": "nova"},
                               headers=alice.auth)
    assert resp.status_code == 200, resp.text
    assert resp.content == provider.speech and resp.headers["content-type"] == "audio/mpeg"
    assert resp.headers["cache-control"] == "no-store"
    [usage] = await h.rows(UsageEvent)
    assert (usage.provider, usage.model, usage.tokens_or_units) == ("voice:cloud", "tts-1", len("Your task is done."))

    too_long = await h.client.post(f"{VOICE}/synthesize", json={"text": "x" * 2001}, headers=alice.auth)
    assert too_long.status_code == 422 and error(too_long)["details"]["reason"] == "text_too_long"


@pytest.mark.parametrize(("media_type", "body", "reason"), [
    ("text/plain", WAV, "audio_type_unsupported"),
    ("application/octet-stream", WAV, "audio_type_unsupported"),
    ("audio/wav", b"", "audio_empty"),
])
async def test_malformed_audio_is_refused_before_any_provider_call(make_harness, provider, media_type, body,
                                                                   reason):
    h = await make_harness(config=server_voice(), transport=provider.transport)
    alice = await h.user("alice")
    resp = await h.client.post(f"{VOICE}/transcribe", content=body, headers=audio_headers(alice, media_type))
    assert resp.status_code == 422, resp.text
    assert error(resp)["details"]["reason"] == reason
    assert provider.requests == []


async def test_oversized_audio_is_refused_at_the_bound(make_harness, provider):
    h = await make_harness(config=server_voice(max_audio_bytes=1000), transport=provider.transport)
    alice = await h.user("alice")
    resp = await h.client.post(f"{VOICE}/transcribe", content=b"\x01" * 1001, headers=audio_headers(alice))
    assert resp.status_code == 422 and error(resp)["details"]["reason"] == "audio_too_large"
    assert provider.requests == []


async def test_language_selection_is_validated_and_forwarded(make_harness, provider):
    h = await make_harness(config=server_voice(), transport=provider.transport)
    alice = await h.user("alice")
    bad = await h.client.post(f"{VOICE}/transcribe", params={"language": "EN;drop"}, content=WAV,
                              headers=audio_headers(alice))
    assert bad.status_code == 422 and provider.requests == []
    ok = await h.client.post(f"{VOICE}/transcribe", params={"language": "hi"}, content=WAV,
                             headers=audio_headers(alice))
    assert ok.status_code == 200
    assert b'name="language"\r\n\r\nhi' in provider.bodies[-1]


async def test_transcripts_are_bounded(make_harness, provider):
    provider.transcript = "word " * 5000
    h = await make_harness(config=server_voice(max_transcript_chars=100), transport=provider.transport)
    alice = await h.user("alice")
    body = (await h.client.post(f"{VOICE}/transcribe", content=WAV, headers=audio_headers(alice))).json()
    assert len(body["transcript"]) == 100 and body["truncated"] is True


async def test_a_paid_provider_is_refused_over_budget(make_harness, provider):
    config = server_voice()
    config["security"]["budgets"] = {"per_user_daily_cost_limit": 0.0, "global_daily_cost_limit": 0.0}
    h = await make_harness(config=config, transport=provider.transport)
    alice = await h.user("alice")
    resp = await h.client.post(f"{VOICE}/transcribe", content=WAV, headers=audio_headers(alice))
    assert resp.status_code == 429 and error(resp)["details"]["limit"] == "per_user_budget"
    assert provider.requests == []


async def test_provider_failures_are_explicit_and_still_metered(make_harness, provider):
    provider.status = 500
    h = await make_harness(config=server_voice(), transport=provider.transport)
    alice = await h.user("alice")
    resp = await h.client.post(f"{VOICE}/transcribe", content=WAV, headers=audio_headers(alice))
    assert resp.status_code == 503
    assert error(resp)["details"] == {"dependency": "voice", "reason": "stt_provider:http_500"}
    [usage] = await h.rows(UsageEvent)
    assert usage.estimated_cost == 0.0  # one event per call, failure included (USAGE-001)
    assert await h.rows(AuditEvent, AuditEvent.action == AuditAction.VOICE_REFUSED.value)
