"""Voice, assembled (docs/27): providers from configuration, keys by reference.

* A server voice provider's key is its `secret_ref`, resolved at the moment of
  the call — from an environment variable, or from the SecretStore as a handle
  of class `model_api_key` only (a voice provider is a model provider). Any
  other class is refused even when the handle exists, so voice configuration
  can never be pointed at a device credential or a push key (12 §2).
* `server.voice` never imports the SecretStore; the key reaches an adapter only
  as an opaque callable (the same seam as `06` §1's model adapters).
* Errors map to 02 §1.7: placement or provider trouble → `503
  dependency_unavailable` with `dependency: voice`; bad input → `422`; limits →
  `429`.
"""

from __future__ import annotations

import contextlib
from typing import Iterator

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from server.composition.secret_context import CURRENT_SECRET_RESOLVER, SecretUnavailable, key_provider_for
from server.config.schema import AppConfig, VoiceProviderConfig
from server.gateway.errors import AppError
from server.gateway.security import SecurityCore
from server.secrets.audit_port import SecretAuditEvent
from server.secrets.requester import SecretRequester
from server.security.audit import AuditLogger
from server.security.usage import UsagePolicy
from server.storage.models import SecretReference
from server.voice.audio import AudioBuffer, normalize_media_type
from server.voice.openai_compatible import OpenAICompatibleSpeechToText, OpenAICompatibleTextToSpeech
from server.voice.service import VoiceRefused, VoiceService, placement_of
from shared.schemas.authorization import Principal
from shared.schemas.enums import AuditResult, SecretClass
from shared.schemas.errors import ErrorCode
from shared.schemas.voice import SynthesisRequest, VoiceConfigView, VoicePlacement, VoiceTranscription


def _error(refusal: VoiceRefused) -> AppError:
    if refusal.kind == "rate_limited":
        return AppError(ErrorCode.RATE_LIMITED, "a usage limit was reached",
                        details={"limit": refusal.reason, "retry_after": refusal.retry_after})
    if refusal.kind == "invalid":
        return AppError(ErrorCode.VALIDATION_FAILED, "the voice request is not valid",
                        details={"reason": refusal.reason})
    return AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "voice is not available for this request",
                    details={"dependency": "voice", "reason": refusal.reason})


class VoiceFacade:
    def __init__(self, *, service: VoiceService, core: SecurityCore) -> None:
        self._service = service
        self._core = core

    @property
    def service(self) -> VoiceService:
        return self._service

    def view(self) -> VoiceConfigView:
        return self._service.view()

    def open_audio(self, media_type: str) -> AudioBuffer:
        normalized = normalize_media_type(media_type)
        if normalized is None:
            raise AppError(ErrorCode.VALIDATION_FAILED, "send the audio with an audio/* content type",
                           details={"reason": "audio_type_unsupported"})
        return AudioBuffer(media_type=normalized, max_bytes=self._service.config.max_audio_bytes)

    @contextlib.contextmanager
    def _key_scope(self, session: AsyncSession, audit: AuditLogger) -> Iterator[None]:
        store = self._core.secret_store

        async def resolve(handle: str, requester: SecretRequester) -> str:
            reference = await session.get(SecretReference, handle)
            if reference is not None and reference.class_ is not SecretClass.MODEL_API_KEY:
                await audit.record_secret_event(SecretAuditEvent(
                    action="secret.get", secret_ref=handle, requester=requester.describe(),
                    result=AuditResult.BLOCKED, reason="not_a_model_api_key",
                ))
                raise SecretUnavailable()
            return await store.get(session, handle, requester, audit)

        token = CURRENT_SECRET_RESOLVER.set(resolve)
        try:
            yield
        finally:
            CURRENT_SECRET_RESOLVER.reset(token)

    async def transcribe(
        self, session: AsyncSession, *, principal: Principal, audio: AudioBuffer, language: str | None,
        audit: AuditLogger,
    ) -> VoiceTranscription:
        with self._key_scope(session, audit):
            try:
                return await self._service.transcribe(session, principal=principal, audio=audio,
                                                      language=language, audit=audit)
            except VoiceRefused as refusal:
                raise _error(refusal) from None

    async def synthesize(
        self, session: AsyncSession, *, principal: Principal, request: SynthesisRequest, audit: AuditLogger
    ) -> tuple[bytes, str]:
        with self._key_scope(session, audit):
            try:
                audio = await self._service.synthesize(session, principal=principal, request=request, audit=audit)
            except VoiceRefused as refusal:
                raise _error(refusal) from None
        return audio.audio, audio.media_type


def _provider(config: AppConfig, provider_id: str | None) -> VoiceProviderConfig | None:
    return next((p for p in config.voice.providers if p.id == provider_id), None)


def build_voice(
    config: AppConfig, *, core: SecurityCore, usage: UsagePolicy,
    transport: httpx.AsyncBaseTransport | None = None,
) -> VoiceFacade:
    """Only the directions placed on a server provider get an adapter; `device`
    and `null` build nothing (docs/27 §0 rule 2)."""

    voice = config.voice
    stt = tts = None
    if placement_of(voice.stt) is VoicePlacement.SERVER:
        entry = _provider(config, voice.stt)
        assert entry is not None and entry.stt_model is not None  # enforced by VoiceConfig
        stt = OpenAICompatibleSpeechToText(
            provider_id=entry.id, endpoint=entry.endpoint, model=entry.stt_model,
            key_provider=key_provider_for(entry.secret_ref, SecretRequester.server()), transport=transport,
        )
    if placement_of(voice.tts) is VoicePlacement.SERVER:
        entry = _provider(config, voice.tts)
        assert entry is not None and entry.tts_model is not None
        tts = OpenAICompatibleTextToSpeech(
            provider_id=entry.id, endpoint=entry.endpoint, model=entry.tts_model, default_voice=entry.tts_voice,
            key_provider=key_provider_for(entry.secret_ref, SecretRequester.server()), transport=transport,
        )
    return VoiceFacade(service=VoiceService(config=voice, usage=usage, stt=stt, tts=tts), core=core)


__all__ = ["VoiceFacade", "build_voice"]
