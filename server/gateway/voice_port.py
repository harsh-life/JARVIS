"""What the HTTP layer needs from voice (docs/27, 02 §10).

`server.gateway` sits below `server.voice` in the layering (16 §2), so it
declares these Protocols and the composition root supplies the implementation
(`server/composition/voice.py`) on `app.state.voice`. Every method takes the
authenticated `Principal`; nothing here can confirm, step up, or authorize —
voice is an input method, not an identity (docs/27 §0).
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from server.security.audit import AuditLogger
from shared.schemas.authorization import Principal
from shared.schemas.voice import SynthesisRequest, VoiceConfigView, VoiceTranscription


class AudioSink(Protocol):
    """Request-memory audio: bounded, zeroed on release (never a file)."""

    def try_append(self, chunk: bytes) -> bool: ...

    def release(self) -> None: ...


class VoicePort(Protocol):
    def view(self) -> VoiceConfigView: ...

    def open_audio(self, media_type: str) -> AudioSink:
        """A bounded in-memory buffer for one upload. Raises `AppError(422)`
        for a media type that is not audio a provider may be sent."""
        ...

    async def transcribe(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        audio: AudioSink,
        language: str | None,
        audit: AuditLogger,
    ) -> VoiceTranscription: ...

    async def synthesize(
        self, session: AsyncSession, *, principal: Principal, request: SynthesisRequest, audit: AuditLogger
    ) -> tuple[bytes, str]: ...


__all__ = ["AudioSink", "VoicePort"]
