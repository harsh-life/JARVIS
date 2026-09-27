"""The replaceable voice provider interfaces (docs/27 §1).

```
SpeechToText  : transcribe(audio, language?) -> {transcript, confidence?}
TextToSpeech  : synthesize(text, voice?)     -> audio
```

A provider is chosen by configuration alone (15 §2 "voice providers"). Core
logic depends on these Protocols, never on a vendor. A provider that fails
raises `VoiceProviderUnavailable` — the caller reports it; no other provider is
silently substituted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from server.voice.audio import AudioBuffer


class VoiceProviderUnavailable(Exception):
    """The provider could not answer. `reason` is an identifier, never a
    response body (which could echo a credential or the user's audio)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Transcript:
    text: str
    confidence: float | None = None
    language: str | None = None


@dataclass(frozen=True)
class SynthesizedAudio:
    audio: bytes
    media_type: str

    def __repr__(self) -> str:
        return f"SynthesizedAudio({self.media_type}, {len(self.audio)} bytes)"


class SpeechToText(Protocol):
    provider_id: str
    model: str

    async def transcribe(self, audio: AudioBuffer, *, language: str | None, timeout: float) -> Transcript: ...


class TextToSpeech(Protocol):
    provider_id: str
    model: str

    async def synthesize(self, text: str, *, voice: str | None, timeout: float, max_bytes: int) -> SynthesizedAudio: ...


__all__ = ["SpeechToText", "SynthesizedAudio", "TextToSpeech", "Transcript", "VoiceProviderUnavailable"]
