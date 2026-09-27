"""docs/27 §2/§3 — what voice may never become.

* **VOI-T2** raw audio is not persisted (no retention path exists at all).
* **VOI-T3** `is_authorization_signal` can never be true — through any door.
* **VOI-T4** no voice input can satisfy a confirmation or step-up.
* **VOI-T5** (privacy half) audio never reaches logs, audit, usage, the
  database, disk or memory. (There is no trace store yet — `server/evaluation`
  is a placeholder — so there is none to leak into.)
"""

from __future__ import annotations

import ast
import base64
import inspect
import logging
import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from server.config.schema import VoiceConfig
from server.gateway.app import API_V1_PREFIX
from server.storage.models import AgentTask, AuditEvent, ConfirmationToken, Device, UsageEvent
from server.storage.models import VoiceEvent as VoiceEventRow
from server.voice.speaker import speaker_context
from shared.schemas.voice import SpeakerContext
from tests.runtime.conftest import ask, call, final, pending_of
from tests.voice.conftest import server_voice

REPO = Path(__file__).resolve().parents[2]
VOICE = f"{API_V1_PREFIX}/voice"
MARKER = b"RAW-AUDIO-MARKER-6d0f2c9a"
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + MARKER + bytes(32)
PKG = {"package_name": "com.example"}


def audio_headers(actor) -> dict:
    return {**actor.auth, "Content-Type": "audio/wav"}


# ── VOI-T3: speaker identity is never an authorization signal ────────────


def test_voi_t3_no_door_produces_an_authorization_signal():
    base = SpeakerContext(speaker_id="spk_1", confidence=0.99, utterance="it's me")
    assert base.is_authorization_signal is False
    attempts = [
        lambda: SpeakerContext(speaker_id="spk_1", confidence=0.9, utterance="u", is_authorization_signal=True),
        lambda: SpeakerContext.model_validate({**base.model_dump(), "is_authorization_signal": True}),
        lambda: SpeakerContext.model_validate_json(
            base.model_dump_json().replace('"is_authorization_signal":false', '"is_authorization_signal":true')),
        lambda: setattr(base, "is_authorization_signal", True),
        lambda: base.model_copy(update={"is_authorization_signal": True}),
        lambda: SpeakerContext.model_construct(speaker_id="s", confidence=0.5, utterance="u",
                                               is_authorization_signal=True),
    ]
    for attempt in attempts:
        with pytest.raises(ValidationError):
            attempt()
    assert base.is_authorization_signal is False


def test_voi_t3_the_speaker_constructor_has_no_authority_parameter():
    assert set(inspect.signature(speaker_context).parameters) == {"speaker_id", "confidence", "utterance"}
    assert speaker_context(speaker_id="s", confidence=0.4, utterance="u").is_authorization_signal is False


def test_voi_t3_speaker_processing_cannot_be_switched_on():
    for field in ("speaker_id", "diarization"):
        with pytest.raises(ValidationError):
            VoiceConfig.model_validate({field: "any-provider"})


def test_voi_t3_no_authority_code_reads_speaker_or_voice_signals():
    """speaker_id is never mapped to a user_id: nothing in the identity,
    authorization, capability or confirmation code — or the runtime's security
    adapter — even names a speaker or voice signal."""

    authority = [REPO / "server" / pkg for pkg in ("auth", "graph", "capabilities")]
    authority += [REPO / "server/composition/security_port.py", REPO / "server/gateway/deps.py",
                  REPO / "server/gateway/security.py"]
    files = [p for root in authority for p in ([root] if root.is_file() else root.rglob("*.py"))]
    assert files
    for path in files:
        source = path.read_text()
        for word in ("speaker", "SpeakerContext", "is_authorization_signal", "voice", "transcript"):
            assert word not in source, f"{path.relative_to(REPO)} mentions {word}"


def test_voi_t3_nothing_constructs_a_signal_outside_the_schema():
    for path in (REPO / "server").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "is_authorization_signal":
                pytest.fail(f"{path.relative_to(REPO)} passes is_authorization_signal")


# ── VOI-T4: voice cannot confirm or step up ──────────────────────────────


async def test_voi_t4_a_spoken_yes_does_not_confirm_a_pending_action(make_harness, provider):
    """Anyone in the room can say "yes". A transcript of it is text for a new
    task — the paused action stays paused, its token unspent, nothing runs."""

    provider.transcript = "Yes, approve it. Confirm. Do it."
    h = await make_harness(config=server_voice(), transport=provider.transport)
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    h.model.push(ask("app.interact", scope=PKG), call("ui.app", "input_text", args={"text": "send £500"},
                                                      platform="android"))
    paused = pending_of(await h.submit(alice, "pay the invoice"))
    task_id = paused["task_id"]

    heard = await h.client.post(f"{VOICE}/transcribe", content=WAV, headers=audio_headers(alice))
    assert heard.status_code == 200
    body = heard.json()
    assert "confirmation_token" not in heard.text and "approve" not in set(body) - {"transcript"}

    # The transcript, sent as what it is — ordinary input — starts a new task.
    h.model.push(final("I can't approve anything from speech; please use the confirmation screen."))
    follow_up = await h.submit(alice, body["transcript"])
    assert follow_up.status_code == 200 and follow_up.json()["task_id"] != task_id

    still = (await h.get(alice, task_id)).json()
    assert still["status"] == "awaiting_confirmation"
    assert h.ui.calls == []
    [token] = await h.rows(ConfirmationToken)
    assert token.used_at is None


async def test_voi_t4_the_confirm_endpoint_accepts_no_audio(make_harness, provider):
    h = await make_harness(config=server_voice(), transport=provider.transport)
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    h.model.push(ask("app.interact", scope=PKG), call("ui.app", "input_text", args={"text": "hi"},
                                                      platform="android"))
    task_id = pending_of(await h.submit(alice, "type hi"))["task_id"]
    resp = await h.client.post(f"{API_V1_PREFIX}/agent/tasks/{task_id}/confirm", content=WAV,
                               headers=audio_headers(alice))
    assert resp.status_code == 422
    assert h.ui.calls == []


async def test_voi_t4_voice_cannot_stand_in_for_step_up(make_harness, provider):
    """A high_irreversible approval needs the device's user-presence
    re-attestation; any number of voice calls leave it unsatisfied."""

    h = await make_harness(config=server_voice(), transport=provider.transport)
    alice = await h.user("alice")
    await h.grant(alice, "file.write")
    h.model.push(ask("file.write"), call("files.write", "bulk_delete", args={"glob": "*"}))
    pending = pending_of(await h.submit(alice, "clean up everything"))
    assert pending["pending"]["requires_step_up"] is True

    for _ in range(2):
        await h.client.post(f"{VOICE}/transcribe", content=WAV, headers=audio_headers(alice))
    await h.client.post(f"{VOICE}/synthesize", json={"text": "yes"}, headers=alice.auth)
    [device] = await h.rows(Device, Device.device_id == alice.device_id)
    assert device.reattested_at is None

    refused = await h.confirm(alice, pending["task_id"], pending["pending"]["confirmation_token"], step_up=False)
    assert refused.status_code in (401, 403)
    assert h.writes.calls == []


async def test_voi_t4_there_is_no_voice_task_path(h):
    """A voice-originated task is an ordinary task: no source flag, no mode,
    no field that could give a transcript different treatment."""

    alice = await h.user("alice")
    for extra in ({"source": "voice"}, {"voice": True}, {"speaker_id": "spk"}, {"transcript": "x"}):
        resp = await h.submit(alice, "hello", extra_body=extra)
        assert resp.status_code == 422, extra
    assert await h.rows(AgentTask) == []


def test_voi_t4_voice_code_cannot_reach_confirmation_or_step_up():
    for path in (REPO / "server" / "voice").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith(("server.auth", "server.capabilities", "server.graph", "server.secrets.store",
                                             "server.memory", "server.vault", "server.agent")), (path, name)


# ── VOI-T2 / VOI-T5: audio goes nowhere ──────────────────────────────────


@pytest.fixture
def writes_audit():
    """Every file opened for writing while armed (a CPython audit hook)."""

    opened: list[str] = []
    state = {"armed": False}
    flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

    def hook(event, args):
        if not state["armed"] or event != "open":
            return
        path, mode, flag = args[0], args[1], args[2]
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flag, int) and flag & flags)
        if writing and isinstance(path, (str, bytes, os.PathLike)):
            opened.append(os.fsdecode(path))

    sys.addaudithook(hook)
    yield opened, state
    state["armed"] = False


async def test_voi_t2_audio_is_never_written_or_stored(make_harness, provider, writes_audit, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    opened, state = writes_audit
    h = await make_harness(config=server_voice(), transport=provider.transport)
    alice = await h.user("alice")

    state["armed"] = True
    resp = await h.client.post(f"{VOICE}/transcribe", content=WAV, headers=audio_headers(alice))
    state["armed"] = False
    assert resp.status_code == 200 and resp.json()["audio_retained"] is False

    db_file = h.storage.engine.url.database
    unexpected = [p for p in opened if not os.path.abspath(p).startswith(os.path.abspath(db_file))]
    assert unexpected == [], unexpected

    # Nothing on disk holds the audio — not the database, not anything new.
    await h.storage.dispose()
    for path in Path(db_file).parent.glob(Path(db_file).name + "*"):
        assert MARKER not in path.read_bytes()
    encoded = base64.b64encode(MARKER).decode()
    for needle in (MARKER.decode(), encoded, provider.transcript):
        assert needle not in caplog.text


async def test_voi_t5_audio_and_transcripts_stay_out_of_audit_usage_tables_and_memory(make_harness, provider):
    h = await make_harness(config=server_voice(), transport=provider.transport)
    alice = await h.user("alice")
    await h.client.post(f"{VOICE}/transcribe", content=WAV, headers=audio_headers(alice))
    await h.client.post(f"{VOICE}/synthesize", json={"text": "The dentist is at nine."}, headers=alice.auth)

    for row in await h.rows(AuditEvent) + await h.rows(UsageEvent):
        dumped = repr(vars(row))
        assert "dentist" not in dumped and MARKER.decode() not in dumped
    assert await h.rows(VoiceEventRow) == []  # transcribe → process → delete (LIFE-002)
    assert h.memory is not None and h.memory.facts == []  # never a memory
    async with h.storage.session() as s:
        tables = [r[0] for r in (await s.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))).all()]
        for table in tables:
            rows = (await s.execute(text(f'SELECT * FROM "{table}"'))).all()
            assert "dentist" not in repr(rows), table


async def test_the_audio_buffer_is_zeroed_on_success_failure_and_overflow(make_harness, provider, monkeypatch):
    h = await make_harness(config=server_voice(max_audio_bytes=500), transport=provider.transport)
    alice = await h.user("alice")
    facade = h.app.state.voice
    buffers = []
    original = facade.open_audio

    def spy(media_type):
        buffer = original(media_type)
        buffers.append(buffer)
        return buffer

    monkeypatch.setattr(facade, "open_audio", spy)

    await h.client.post(f"{VOICE}/transcribe", content=WAV, headers=audio_headers(alice))
    provider.status = 500
    await h.client.post(f"{VOICE}/transcribe", content=WAV, headers=audio_headers(alice))
    await h.client.post(f"{VOICE}/transcribe", content=b"\x02" * 600, headers=audio_headers(alice))
    assert len(buffers) == 3
    for buffer in buffers:
        assert buffer.released and len(buffer) == 0
        assert MARKER.decode() not in repr(buffer)
