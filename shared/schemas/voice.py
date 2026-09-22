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

from pydantic import Field

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
    user_id, and MUST NOT be mapped to one for authorization purposes."""

    speaker_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    utterance: str
    timestamp: datetime = Field(default_factory=utcnow)
    is_authorization_signal: Literal[False] = False
