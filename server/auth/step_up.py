"""Step-up by re-attestation (03 §5.5 SESSION-003; docs/23 §3).

03 §5.5 lets "fresh" mean *either* a recent authentication *or* "a
re-attestation challenge the device signs". For approving a `high_irreversible`
action from a device (docs/23 §3, §5.4), token freshness is not enough: the
Android client refreshes its access token in the background with its device
proof, so a token is almost always "fresh" without any person being present.
Here, fresh means the **user** was present on that device a moment ago:

* The device holds a second key — ECDSA P-256 in the Android Keystore, created
  so that **every use** needs the user's biometric or device credential. Only
  its public half (SubjectPublicKeyInfo) is registered here.
* Registration is allowed only in the enrollment window right after the
  device was registered through the interactive Google login, or to replace a
  key with a fresh re-attestation by the old one. A stolen device credential
  alone therefore cannot plant its own step-up key later (03 §5.3's residual
  risk stays bounded).
* A step-up is a single-use, short-lived server challenge signed with that key.
  A verified signature marks the **device** re-attested; the confirm path
  requires that within `REATTESTATION_WINDOW`.

This is not a new authentication scheme: the device credential still
authenticates every request (03 §3). It is the re-attestation 03 §5.5 already
names, bound to user presence by the platform.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.errors import AuthError
from server.auth.repository import as_utc, utcnow
from server.auth.sessions import STEP_UP_WINDOW
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.storage.models import Device
from shared.schemas.enums import AuditActor, AuditResult

ATTESTATION_PREFIX = "hypermind-step-up"
ATTESTATION_VERSION = "v1"
CHALLENGE_TTL = timedelta(seconds=60)
ENROLLMENT_WINDOW = timedelta(minutes=15)
REATTESTATION_WINDOW = STEP_UP_WINDOW


class StepUpRejected(AuthError):
    """A step-up key registration or attestation was refused. One generic
    client message; the reason is for the audit trail only."""

    client_message = "step-up re-attestation failed"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def attestation_message(*, device_id: uuid.UUID | str, challenge: str) -> bytes:
    """What the device signs with its step-up key. Domain-separated from the
    device proof, and bound to exactly one device and one challenge."""

    return f"{ATTESTATION_PREFIX}|{ATTESTATION_VERSION}|{device_id}|{challenge}".encode("utf-8")


def _load_public_key(encoded: str) -> ec.EllipticCurvePublicKey:
    try:
        key = serialization.load_der_public_key(_b64d(encoded))
    except (ValueError, binascii.Error, TypeError) as exc:
        raise StepUpRejected("malformed_public_key") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise StepUpRejected("not_p256")
    return key


def reattested_recently(reattested_at: datetime | None, *, now: datetime | None = None,
                        window: timedelta = REATTESTATION_WINDOW) -> bool:
    if reattested_at is None:
        return False
    return (now or utcnow()) - as_utc(reattested_at) <= window


class StepUpService:
    async def register_key(
        self, session: AsyncSession, *, device: Device, public_key: str, audit: AuditLogger
    ) -> None:
        key = _load_public_key(public_key)
        now = utcnow()
        if device.step_up_public_key is None:
            if now - as_utc(device.registered_at) > ENROLLMENT_WINDOW:
                await self._audit(audit, device, AuditAction.STEP_UP_KEY_REJECTED, AuditResult.BLOCKED)
                raise StepUpRejected("enrollment_window_closed")
        elif not reattested_recently(device.reattested_at, now=now):
            await self._audit(audit, device, AuditAction.STEP_UP_KEY_REJECTED, AuditResult.BLOCKED)
            raise StepUpRejected("reattestation_required")
        device.step_up_public_key = _b64e(
            key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        )
        # A new key starts un-attested: its first use is its own step-up.
        device.reattested_at = None
        device.step_up_challenge_hash = None
        device.step_up_challenge_expires_at = None
        await session.flush()
        await self._audit(audit, device, AuditAction.STEP_UP_KEY_REGISTERED, AuditResult.SUCCESS)

    async def challenge(self, session: AsyncSession, *, device: Device) -> tuple[str, datetime]:
        if device.step_up_public_key is None:
            raise StepUpRejected("no_step_up_key")
        challenge = secrets.token_urlsafe(32)
        expires_at = utcnow() + CHALLENGE_TTL
        # One outstanding challenge per device; a new one replaces the old.
        device.step_up_challenge_hash = hashlib.sha256(challenge.encode("ascii")).hexdigest()
        device.step_up_challenge_expires_at = expires_at
        await session.flush()
        return challenge, expires_at

    async def attest(
        self, session: AsyncSession, *, device: Device, challenge: str, signature: str, audit: AuditLogger
    ) -> datetime:
        expected_hash = device.step_up_challenge_hash
        expires_at = device.step_up_challenge_expires_at
        # Single use, whatever the outcome.
        device.step_up_challenge_hash = None
        device.step_up_challenge_expires_at = None
        await session.flush()
        now = utcnow()
        presented = hashlib.sha256(challenge.encode("utf-8")).hexdigest()
        reason = None
        if device.step_up_public_key is None:
            reason = "no_step_up_key"
        elif expected_hash is None or not hmac.compare_digest(presented, expected_hash):
            reason = "unknown_challenge"
        elif expires_at is None or now >= as_utc(expires_at):
            reason = "expired_challenge"
        else:
            try:
                _load_public_key(device.step_up_public_key).verify(
                    _b64d(signature),
                    attestation_message(device_id=device.device_id, challenge=challenge),
                    ec.ECDSA(hashes.SHA256()),
                )
            except (InvalidSignature, ValueError, binascii.Error):
                reason = "bad_signature"
        if reason is not None:
            await self._audit(audit, device, AuditAction.STEP_UP_ATTESTATION_FAILED, AuditResult.BLOCKED)
            raise StepUpRejected(reason)
        device.reattested_at = now
        await session.flush()
        await self._audit(audit, device, AuditAction.STEP_UP_ATTESTED, AuditResult.SUCCESS)
        return now + REATTESTATION_WINDOW

    @staticmethod
    async def _audit(audit: AuditLogger, device: Device, action: AuditAction, result: AuditResult) -> None:
        await audit.record(
            actor=AuditActor.USER, action=action, resource=f"device:{device.device_id}", result=result,
            user_id=device.user_id, device_id=device.device_id,
        )


__all__ = [
    "ATTESTATION_PREFIX",
    "CHALLENGE_TTL",
    "ENROLLMENT_WINDOW",
    "REATTESTATION_WINDOW",
    "StepUpRejected",
    "StepUpService",
    "attestation_message",
    "reattested_recently",
]
