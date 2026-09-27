"""Voice endpoints — 02 §10, docs/27.

`POST /voice/transcribe` takes the raw audio as the request body (`Content-Type:
audio/*`), read as a stream into a bounded in-memory buffer — an oversized
upload is refused as soon as it passes the bound, and nothing is written to
disk. It returns `VoiceEvent{transcript, audio_retained: false}` plus the
provider's confidence. The transcript is the caller's own ordinary input: to act
on it, the client submits it as a normal task. There is no voice route into
confirmation or step-up.

`POST /voice/synthesize` takes `{text, voice?}` and returns audio.

`GET /voice/config` tells a client where each direction runs (`device`,
`server`, `off`) — never a provider id, endpoint or credential.

With a direction placed on the device or off, its endpoint answers `503
dependency_unavailable` (`dependency: voice`) with the reason; no provider is
substituted. Without a voice port at all, every endpoint answers the same.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from server.gateway.deps import get_audit_logger, get_db_session, get_principal
from server.gateway.errors import AppError
from server.gateway.voice_port import VoicePort
from server.security.audit import AuditLogger
from shared.schemas.authorization import Principal
from shared.schemas.errors import ErrorCode
from shared.schemas.voice import SynthesisRequest, VoiceConfigView, VoiceTranscription

router = APIRouter(tags=["voice"])

MAX_LANGUAGE_LENGTH = 16


def _voice(request: Request) -> VoicePort:
    port = getattr(request.app.state, "voice", None)
    if port is None:
        raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "voice is not available on this server",
                       details={"dependency": "voice", "reason": "voice_disabled"})
    return port


@router.get("/voice/config", response_model=VoiceConfigView)
async def voice_config(request: Request, principal: Principal = Depends(get_principal)) -> VoiceConfigView:
    return _voice(request).view()


@router.post("/voice/transcribe", response_model=VoiceTranscription)
async def transcribe(
    request: Request,
    language: str | None = None,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> VoiceTranscription:
    port = _voice(request)
    if language is not None and len(language) > MAX_LANGUAGE_LENGTH:
        raise AppError(ErrorCode.VALIDATION_FAILED, "language is too long")
    audio = port.open_audio(request.headers.get("content-type", ""))
    try:
        async for chunk in request.stream():
            if chunk and not audio.try_append(chunk):
                raise AppError(ErrorCode.VALIDATION_FAILED, "the audio is larger than this server accepts",
                               details={"reason": "audio_too_large"})
        return await port.transcribe(session, principal=principal, audio=audio, language=language, audit=audit)
    finally:
        audio.release()


@router.post("/voice/synthesize")
async def synthesize(
    request: Request,
    body: SynthesisRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
    audit: AuditLogger = Depends(get_audit_logger),
) -> Response:
    audio, media_type = await _voice(request).synthesize(session, principal=principal, request=body, audit=audit)
    return Response(content=audio, media_type=media_type, headers={"Cache-Control": "no-store"})
