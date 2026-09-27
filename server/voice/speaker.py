"""Speaker context — the `[FUTURE]` diarization / speaker-id provider slots
(docs/27 §1, §3).

Nothing here is implemented, and `voice.diarization` / `voice.speaker_id` only
load as `null`. The shape exists so a future provider plugs in behind a fixed
contract: whatever it reports is a `SpeakerContext`, which is **structurally**
not an authorization signal (`Literal[False]`, frozen, and a DB check), and a
`speaker_id` is a provider label — never mapped to a `user_id` (VOICE-002,
INV-14). Detected affect, if a provider ever reports it, is context only and is
never stored as relationship content (EMO-005). The session, not the voice,
identifies the user.
"""

from __future__ import annotations

from typing import Protocol

from shared.schemas.voice import SpeakerContext


class SpeakerIdentifier(Protocol):
    """A future provider: labels who spoke. Context only."""

    async def identify(self, utterance: str) -> SpeakerContext | None: ...


class Diarizer(Protocol):
    """A future provider: splits a transcript by speaker. Context only."""

    async def diarize(self, transcript: str) -> list[SpeakerContext]: ...


def speaker_context(*, speaker_id: str, confidence: float, utterance: str) -> SpeakerContext:
    """The only constructor this package offers: it has no parameter through
    which an authorization signal could be passed."""

    return SpeakerContext(speaker_id=speaker_id, confidence=confidence, utterance=utterance)


__all__ = ["Diarizer", "SpeakerIdentifier", "speaker_context"]
