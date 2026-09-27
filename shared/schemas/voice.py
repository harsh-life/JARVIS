"""Voice entities — VoiceEvent, SpeakerContext.

Source: 01_DATA_MODEL_SCHEMA.md §12. PRD VOICE-001..004, EMO-005.

DM-T8 [LOCKED]: "`is_authorization_signal` cannot be set true (INV-14)."
This is enforced *structurally* below via `Literal[False]` — there is no
code path, anywhere, that can construct a SpeakerContext with this field
true. That is the point: speaker identity is context only, never an
authorization signal, and the type system makes the violation
unrepresentable rather than merely "checked."
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from enum import Enum

from pydantic import ConfigDict, Field

from shared.schemas.common import ORMBase, utcnow


class VoiceEvent(ORMBase):
    """01 §12.1. `audio_retained` defaults False (LIFE-002): transcribe ->
    process -> delete unless the user opts in — foundation represents the
    flag; it does not implement STT or retention/deletion."""

    voice_event_id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    transcript: str
    audio_retained: bool = False
    timestamp: datetime = Field(default_factory=utcnow)


class SpeakerContext(ORMBase):
    """01 §12.1. `speaker_id` is a provider label, never a Hypermind
    user_id, and MUST NOT be mapped to one for authorization purposes.

    Frozen: `Literal[False]` stops construction with `True`, and freezing stops
    assignment after it (pydantic does not validate assignments by default), so
    no instance can ever carry an authorization signal (VOI-T3, INV-14)."""

    model_config = ConfigDict(from_attributes=True, extra="forbid", frozen=True)

    speaker_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    utterance: str
    timestamp: datetime = Field(default_factory=utcnow)
    is_authorization_signal: Literal[False] = False


# ── API shapes (02 §10, docs/27) ─────────────────────────────────────────


class VoicePlacement(str, Enum):
    """Where one voice direction runs (docs/27 §1, §4)."""

    DEVICE = "device"  # Android speech recognition / system TTS; audio never leaves the phone
    SERVER = "server"  # a configured provider behind /api/v1/voice/*
    OFF = "off"  # voice is detachable: Track B works without it (VOI-T1)


class VoiceConfigView(ORMBase):
    """`GET /api/v1/voice/config` — placement only: no provider id, endpoint or
    credential reference reaches a client."""

    stt: VoicePlacement
    tts: VoicePlacement
    max_audio_bytes: int
    max_transcript_chars: int
    max_tts_chars: int


class VoiceTranscription(VoiceEvent):
    """`POST /api/v1/voice/transcribe` → 02 §10's `VoiceEvent`, plus what the
    SpeechToText interface returns (docs/27 §1). `audio_retained` is always
    false: this build has no retention path at all (PRD #27, LIFE-002). The
    transcript is ordinary, untrusted user input; it authorizes nothing and is
    never routed to a confirmation."""

    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    language: str | None = None
    truncated: bool = False


class SynthesisRequest(ORMBase):
    """`POST /api/v1/voice/synthesize` — `{text}` → audio (02 §10)."""

    text: str = Field(min_length=1, max_length=10_000)
    voice: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_\-]{1,64}$")


__all__ = [
    "SpeakerContext",
    "SynthesisRequest",
    "VoiceConfigView",
    "VoiceEvent",
    "VoicePlacement",
    "VoiceTranscription",
]
