"""Server-side voice (docs/27 §1–§3): placement, bounds, metering, audit.

The two rules this module keeps (docs/27 §0):

1. **A voice is an input method, not an identity.** A transcript is returned to
   the authenticated caller as ordinary text; it authorizes nothing, confirms
   nothing, and satisfies no step-up. Nothing here imports the confirmation,
   step-up, authorization or identity code (`pyproject.toml` contracts).
2. **Voice is detachable.** With a direction placed on `device` or `null`, the
   server provider for it simply does not exist, and asking for it is a clean
   `dependency_unavailable` — never a silent substitute.

Every provider call is precheck'd against the usage limits and emits exactly
one `UsageEvent` (`model_call`, USAGE-001), success or failure. Audio lives in
an `AudioBuffer` that is zeroed when the request ends; no transcript, audio or
synthesized text is stored, logged or audited.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from server.config.schema import VOICE_ON_DEVICE, VoiceConfig, VoicePricingConfig
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.security.usage import LimitExceeded, UsagePolicy
from server.voice.audio import AudioBuffer
from server.voice.providers import (
    SpeechToText,
    SynthesizedAudio,
    TextToSpeech,
    VoiceProviderUnavailable,
)
from shared.schemas.authorization import Principal
from shared.schemas.enums import AuditActor, AuditResult, UsageKind
from shared.schemas.voice import SynthesisRequest, VoiceConfigView, VoicePlacement, VoiceTranscription

_LANGUAGE = re.compile(r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")
_MB = 1_000_000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class VoiceRefused(Exception):
    """`kind`: `unavailable` (503), `invalid` (422) or `rate_limited` (429)."""

    def __init__(self, kind: str, reason: str, *, retry_after: int | None = None) -> None:
        super().__init__(reason)
        self.kind = kind
        self.reason = reason
        self.retry_after = retry_after


def placement_of(value: str | None) -> VoicePlacement:
    if value is None:
        return VoicePlacement.OFF
    if value == VOICE_ON_DEVICE:
        return VoicePlacement.DEVICE
    return VoicePlacement.SERVER


class VoiceService:
    def __init__(
        self,
        *,
        config: VoiceConfig,
        usage: UsagePolicy,
        stt: SpeechToText | None = None,
        tts: TextToSpeech | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._config = config
        self._usage = usage
        self._clock = clock
        providers = {p.id: p for p in config.providers}
        self._stt = stt if placement_of(config.stt) is VoicePlacement.SERVER else None
        self._tts = tts if placement_of(config.tts) is VoicePlacement.SERVER else None
        self._stt_config = providers.get(config.stt or "")
        self._tts_config = providers.get(config.tts or "")

    @property
    def config(self) -> VoiceConfig:
        return self._config

    def view(self) -> VoiceConfigView:
        return VoiceConfigView(
            stt=placement_of(self._config.stt), tts=placement_of(self._config.tts),
            max_audio_bytes=self._config.max_audio_bytes, max_transcript_chars=self._config.max_transcript_chars,
            max_tts_chars=self._config.max_tts_chars,
        )

    # ── STT ─────────────────────────────────────────────────────────────

    async def transcribe(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        audio: AudioBuffer,
        language: str | None,
        audit: AuditLogger,
    ) -> VoiceTranscription:
        try:
            stt = self._stt
            if stt is None or self._stt_config is None:
                raise VoiceRefused("unavailable", f"stt_{placement_of(self._config.stt).value}")
            if len(audio) == 0:
                raise VoiceRefused("invalid", "audio_empty")
            if language is not None and not _LANGUAGE.match(language):
                raise VoiceRefused("invalid", "language_invalid")
            cost = _price(self._stt_config.pricing, "per_audio_mb", len(audio) / _MB)
            await self._precheck(session, principal, cost)
            units = max(1, (len(audio) + 1023) // 1024)
            try:
                transcript = await stt.transcribe(audio, language=language,
                                                  timeout=self._stt_config.timeout_seconds)
            except VoiceProviderUnavailable as exc:
                await self._meter(session, principal, audit, stt.provider_id, stt.model, units, 0.0)
                raise VoiceRefused("unavailable", f"stt_provider:{exc.reason}") from None
            await self._meter(session, principal, audit, stt.provider_id, stt.model, units, cost)
        except VoiceRefused as refusal:
            await self._refused(audit, principal, refusal)
            raise
        finally:
            audio.release()

        text = transcript.text.strip()
        limit = self._config.max_transcript_chars
        truncated = len(text) > limit
        event_id = uuid.uuid4()
        await audit.record(
            actor=AuditActor.USER, action=AuditAction.VOICE_TRANSCRIBED, resource=f"voice_event:{event_id}",
            result=AuditResult.SUCCESS, user_id=principal.user_id, device_id=principal.device_id,
            session_id=principal.session_id,
        )
        return VoiceTranscription(
            voice_event_id=event_id, session_id=principal.session_id, transcript=text[:limit],
            audio_retained=False, timestamp=self._clock(), confidence=transcript.confidence,
            language=transcript.language or language, truncated=truncated,
        )

    # ── TTS ─────────────────────────────────────────────────────────────

    async def synthesize(
        self, session: AsyncSession, *, principal: Principal, request: SynthesisRequest, audit: AuditLogger
    ) -> SynthesizedAudio:
        try:
            tts = self._tts
            if tts is None or self._tts_config is None:
                raise VoiceRefused("unavailable", f"tts_{placement_of(self._config.tts).value}")
            text = request.text.strip()
            if not text:
                raise VoiceRefused("invalid", "text_empty")
            if len(text) > self._config.max_tts_chars:
                raise VoiceRefused("invalid", "text_too_long")
            cost = _price(self._tts_config.pricing, "per_1k_chars", len(text) / 1000)
            await self._precheck(session, principal, cost)
            try:
                audio = await tts.synthesize(text, voice=request.voice, timeout=self._tts_config.timeout_seconds,
                                             max_bytes=self._config.max_tts_audio_bytes)
            except VoiceProviderUnavailable as exc:
                await self._meter(session, principal, audit, tts.provider_id, tts.model, len(text), 0.0)
                raise VoiceRefused("unavailable", f"tts_provider:{exc.reason}") from None
            await self._meter(session, principal, audit, tts.provider_id, tts.model, len(text), cost)
        except VoiceRefused as refusal:
            await self._refused(audit, principal, refusal)
            raise
        await audit.record(
            actor=AuditActor.USER, action=AuditAction.VOICE_SYNTHESIZED, resource=f"voice_synthesis:{uuid.uuid4()}",
            result=AuditResult.SUCCESS, user_id=principal.user_id, device_id=principal.device_id,
            session_id=principal.session_id,
        )
        return audio

    # ── shared ──────────────────────────────────────────────────────────

    async def _precheck(self, session: AsyncSession, principal: Principal, cost: float) -> None:
        try:
            await self._usage.precheck(session, user_id=principal.user_id, device_id=principal.device_id,
                                       projected_cost=cost)
        except LimitExceeded as exc:
            raise VoiceRefused("rate_limited", exc.limit, retry_after=exc.retry_after_seconds) from None

    async def _meter(self, session: AsyncSession, principal: Principal, audit: AuditLogger, provider_id: str,
                     model: str, units: int, cost: float) -> None:
        await self._usage.ledger.record(
            session, request_id=audit.request_id, user_id=principal.user_id, device_id=principal.device_id,
            session_id=principal.session_id, kind=UsageKind.MODEL_CALL, provider=f"voice:{provider_id}",
            model=model, units=units, estimated_cost=cost,
        )

    @staticmethod
    async def _refused(audit: AuditLogger, principal: Principal, refusal: VoiceRefused) -> None:
        await audit.record(
            actor=AuditActor.USER, action=AuditAction.VOICE_REFUSED, resource=f"voice:refused:{refusal.reason}",
            result=AuditResult.BLOCKED, user_id=principal.user_id, device_id=principal.device_id,
            session_id=principal.session_id,
        )


def _price(pricing: VoicePricingConfig | None, field: str, amount: float) -> float:
    if pricing is None:
        return 0.0
    return max(0.0, float(getattr(pricing, field)) * amount)


__all__ = ["VoiceRefused", "VoiceService", "placement_of"]
