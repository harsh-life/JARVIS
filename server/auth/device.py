"""Device identity and the device-credential lifecycle (03 §3, §4; DEVICE-001).

The load-bearing rule (03 §0, PHONE-003):

> **the client is never trusted to assert who it is.** Every identity fact the
> server acts on is derived from a validated credential the server itself issued
> or a token it can cryptographically verify — never from a `user_id`/
> `device_id`/`session_id` in a request body.

So nothing in this module takes a caller-supplied `user_id`. A device's owning
user comes from the `Device` row, which came from a spent bootstrap token, which
came from a validated id_token.

## Resolving OD-D1: asymmetric credentials

03 §4.2 leaves the credential's form `[IMPL]` and `[REC]`s asymmetric, and
12 §3 repeats the recommendation:

> prefer an **asymmetric device credential** (device holds a private key; server
> stores only the public key/verifier here) so that even a full SecretStore leak
> does not yield working device credentials — the server never held the
> impersonating secret.

That is what this implements, with Ed25519. The consequences worth being precise
about:

* The value returned exactly once at registration (03 §3.1 step 5) is the
  device's **private** key. The server keeps only the public verifier, in the
  SecretStore under `class=device_credential` as 03 §3.1 step 4 specifies.
* A full database *and* SecretStore compromise therefore yields no material that
  can impersonate any device — the server never possessed it (03 §7's
  "Server DB leak → impersonation" row, BLAST-002).

## Presenting the credential

02 §3 gives the refresh call one field, `{device_credential}`. A bare secret in
that field would be replayable by anyone who captured it, so what the client
sends is a **proof of possession**: a short self-signed assertion carrying the
device id, a fresh nonce, and a timestamp. `[IMPL]`, within 03 §4.2's locked
guarantee (the server can validate and revoke, and a DB leak yields nothing
usable) — chosen over a server-issued challenge because that would need a second
round trip and a second endpoint 02 §3 does not define.

Replay is closed by the nonce being single-use inside a short freshness window
(`device_proof_nonces`), so a captured proof is useless.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.bootstrap import consume_bootstrap_token
from server.auth.errors import InvalidDeviceProof
from server.auth.repository import AuthRepository, utcnow
from server.secrets.errors import SecretStoreError
from server.secrets.requester import SecretRequester
from server.secrets.store import SecretStore
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.storage.models import Device, DeviceProofNonce, User
from shared.schemas.enums import (
    AuditActor,
    AuditResult,
    DevicePlatform,
    SecretClass,
    SecretOwnerScopeType,
    UserStatus,
)

PROOF_VERSION = "v1"
PROOF_PREFIX = "hypermind-device-proof"
REGISTRATION_PREFIX = "hypermind-device-register"
ROTATION_PREFIX = "hypermind-device-rotate"

# How stale a proof may be. Wide enough for ordinary clock drift and network
# latency, narrow enough that the replay-nonce table stays small and a captured
# proof is quickly worthless even before the nonce check.
PROOF_FRESHNESS = timedelta(seconds=120)

# Nonces are retained a little longer than the freshness window, so a proof that
# is still *within* the window always finds its nonce already recorded.
NONCE_RETENTION = PROOF_FRESHNESS * 2


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _canonical_message(*, device_id: uuid.UUID | str, nonce: str, issued_at: int) -> bytes:
    """What the device signs.

    Every field the server checks is inside the signature, so none of them can
    be altered in transit: changing the device id, the nonce, or the timestamp
    invalidates the signature rather than redirecting the proof. The fixed
    prefix domain-separates this signature from any other use of the same key.
    """

    return f"{PROOF_PREFIX}|{PROOF_VERSION}|{device_id}|{nonce}|{issued_at}".encode("utf-8")


@dataclass(frozen=True)
class RegisteredDevice:
    """03 §3.1 step 5's response.

    `device_credential` is the raw private key, returned **exactly once**. It is
    not stored anywhere server-side, is never re-fetchable, and never appears in
    a log, audit record, or usage record (SECRET-004, AUTH-T4). A lost credential
    is handled by revoke-and-re-register, never by re-fetch (02 §3).

    It is `None` when the device generated its own key pair and registered only
    the public half (docs/23 §3: the private key lives in the Android Keystore
    and never exists anywhere else) — there is then nothing to return.
    """

    device: Device
    device_credential: str | None


def registration_message(*, bootstrap_token: str, public_key: str) -> bytes:
    """What a device signs, with the key it is registering, to prove it holds
    that key. Binding the bootstrap token's digest means the signature is
    useless for any other registration; the token itself never appears."""

    digest = hashlib.sha256(bootstrap_token.encode("utf-8")).hexdigest()
    return f"{REGISTRATION_PREFIX}|{PROOF_VERSION}|{digest}|{public_key}".encode("utf-8")


def rotation_message(*, device_id: uuid.UUID | str, public_key: str) -> bytes:
    """What a device signs with its *new* key when rotating to it."""

    return f"{ROTATION_PREFIX}|{PROOF_VERSION}|{device_id}|{public_key}".encode("utf-8")


def _verified_public_key(public_key: str, key_proof: str, message: bytes) -> str:
    """Decode a device-supplied Ed25519 public key and check the device signed
    `message` with the matching private key. Returns the key re-encoded
    canonically. Any failure is the same `InvalidDeviceProof`."""

    try:
        raw = _b64d(public_key)
        signature = _b64d(key_proof)
    except (ValueError, binascii.Error) as exc:
        raise InvalidDeviceProof("malformed_public_key") from exc
    if len(raw) != 32:
        raise InvalidDeviceProof("malformed_public_key")
    try:
        Ed25519PublicKey.from_public_bytes(raw).verify(signature, message)
    except (InvalidSignature, ValueError) as exc:
        raise InvalidDeviceProof("key_not_held") from exc
    return _b64e(raw)


@dataclass(frozen=True)
class DeviceProof:
    device_id: uuid.UUID
    nonce: str
    issued_at: int
    signature: bytes

    @classmethod
    def parse(cls, value: str) -> "DeviceProof":
        if not value:
            raise InvalidDeviceProof("absent")
        parts = value.split(".")
        if len(parts) != 5:
            raise InvalidDeviceProof("malformed")
        version, device_id, nonce, issued_at, signature = parts
        if version != PROOF_VERSION:
            raise InvalidDeviceProof("unsupported_version")
        if not nonce:
            raise InvalidDeviceProof("malformed")
        try:
            return cls(
                device_id=uuid.UUID(device_id),
                nonce=nonce,
                issued_at=int(issued_at),
                signature=_b64d(signature),
            )
        except (ValueError, binascii.Error) as exc:
            raise InvalidDeviceProof("malformed") from exc

    def message(self) -> bytes:
        return _canonical_message(
            device_id=self.device_id, nonce=self.nonce, issued_at=self.issued_at
        )


def build_device_proof(
    *, device_id: uuid.UUID, device_credential: str, issued_at: int | None = None
) -> str:
    """Construct a proof from a device credential.

    Lives server-side because the Android client is not this repository's code
    and the *format* must be specified somewhere executable — this is the
    normative implementation the client mirrors, and what the tests sign with.
    It is never called with a credential the server holds: the server has only
    public keys.
    """

    try:
        private_key = Ed25519PrivateKey.from_private_bytes(_b64d(device_credential))
    except (ValueError, binascii.Error) as exc:
        raise InvalidDeviceProof("malformed_credential") from exc

    nonce = _b64e(uuid.uuid4().bytes)
    stamp = int(datetime.now(timezone.utc).timestamp()) if issued_at is None else issued_at
    signature = private_key.sign(
        _canonical_message(device_id=device_id, nonce=nonce, issued_at=stamp)
    )
    return f"{PROOF_VERSION}.{device_id}.{nonce}.{stamp}.{_b64e(signature)}"


class DeviceService:
    def __init__(
        self,
        *,
        secret_store: SecretStore,
        repository: AuthRepository | None = None,
    ) -> None:
        self._store = secret_store
        self._repo = repository or AuthRepository()

    # ── 03 §3 registration ──────────────────────────────────────────────

    async def register(
        self,
        session: AsyncSession,
        *,
        bootstrap_token: str,
        platform: DevicePlatform,
        audit: AuditLogger,
        public_key: str | None = None,
        key_proof: str | None = None,
    ) -> RegisteredDevice:
        """03 §3.1, in the document's own order.

        The bootstrap token is spent first: if registration fails afterwards, the
        token is still consumed, so a failed attempt cannot be retried into two
        devices. The user must re-authenticate — the safe direction.

        With `public_key` (and `key_proof`, its signature over
        `registration_message`), the device supplies its own key pair's public
        half and the server never holds a private key at all (03 §4.2's
        asymmetric option, docs/23 §3's Keystore-held key). Without it, the
        server mints the key pair and returns the private half once.
        """

        if (public_key is None) != (key_proof is None):
            raise InvalidDeviceProof("incomplete_public_key")
        verified_public: str | None = None
        if public_key is not None and key_proof is not None:
            # Checked before the token is spent, so a malformed request does not
            # burn the user's login.
            verified_public = _verified_public_key(
                public_key,
                key_proof,
                registration_message(bootstrap_token=bootstrap_token, public_key=public_key),
            )

        try:
            user_id = await consume_bootstrap_token(session, bootstrap_token)
        except Exception:
            await audit.record(
                actor=AuditActor.SYSTEM,
                action=AuditAction.BOOTSTRAP_TOKEN_REJECTED,
                resource="device:*",
                result=AuditResult.BLOCKED,
            )
            raise

        private_key = Ed25519PrivateKey.generate() if verified_public is None else None
        public_b64 = (
            verified_public
            if verified_public is not None
            else _b64e(private_key.public_key().public_bytes_raw())
        )

        device = Device(
            user_id=user_id,
            platform=platform,
            # Set below, once the SecretStore has minted the handle. The column
            # is non-nullable, so a placeholder would be a lie; the row is added
            # after the handle exists.
            credential_ref="",
            registered_at=utcnow(),
        )

        secret_ref = await self._store.set(
            session,
            owner_scope_type=SecretOwnerScopeType.USER,
            owner_scope_id=str(user_id),
            secret_class=SecretClass.DEVICE_CREDENTIAL,
            # Only the public verifier. The private key below never reaches the
            # store, the database, or a log.
            value=public_b64,
            requester=SecretRequester.server(),
            audit=audit,
        )
        device.credential_ref = secret_ref
        session.add(device)
        await session.flush()

        await audit.record(
            actor=AuditActor.USER,
            action=AuditAction.DEVICE_REGISTERED,
            resource=f"device:{device.device_id}",
            result=AuditResult.SUCCESS,
            user_id=user_id,
            device_id=device.device_id,
        )

        return RegisteredDevice(
            device=device,
            device_credential=(
                _b64e(private_key.private_bytes_raw()) if private_key is not None else None
            ),
        )

    # ── 03 §4 validation ────────────────────────────────────────────────

    async def verify_proof(
        self, session: AsyncSession, raw_proof: str, *, audit: AuditLogger
    ) -> Device:
        """Validate a device-credential proof and return the device.

        Every rejection path in 03 §5.4 lands here — device revoked, credential
        rotated (so the old private key no longer verifies), credential invalid,
        or user `status != active` — and all of them raise the same
        `InvalidDeviceProof`, so a client learns only that it must sign in again.
        """

        try:
            device = await self._verify(session, raw_proof, audit)
        except Exception:
            await audit.record(
                actor=AuditActor.SYSTEM,
                action=AuditAction.DEVICE_PROOF_REJECTED,
                resource="device:*",
                result=AuditResult.BLOCKED,
            )
            raise

        device.last_seen = utcnow()
        await session.flush()
        return device

    async def _verify(
        self, session: AsyncSession, raw_proof: str, audit: AuditLogger
    ) -> Device:
        proof = DeviceProof.parse(raw_proof)

        device = await self._repo.get_device(session, proof.device_id)
        if device is None:
            raise InvalidDeviceProof("unknown_device")

        # 03 §4.4: revocation is immediate — checked before any cryptography, so
        # a revoked device's still-valid signature buys nothing (AUTH-T5).
        if device.revoked:
            raise InvalidDeviceProof("device_revoked")

        user = await session.get(User, device.user_id)
        if user is None or user.status is not UserStatus.ACTIVE:
            raise InvalidDeviceProof("user_not_active")

        now = datetime.now(timezone.utc)
        issued_at = datetime.fromtimestamp(proof.issued_at, tz=timezone.utc)
        if abs((now - issued_at).total_seconds()) > PROOF_FRESHNESS.total_seconds():
            raise InvalidDeviceProof("stale_proof")

        await self._record_nonce(session, device_id=device.device_id, nonce=proof.nonce, now=now)

        public_raw = await self._resolve_verifier(session, device, audit)
        try:
            Ed25519PublicKey.from_public_bytes(public_raw).verify(
                proof.signature, proof.message()
            )
        except (InvalidSignature, ValueError) as exc:
            raise InvalidDeviceProof("bad_signature") from exc

        return device

    async def _record_nonce(
        self, session: AsyncSession, *, device_id: uuid.UUID, nonce: str, now: datetime
    ) -> None:
        """Single-use nonce — the replay defense (03 §7).

        Recorded via the primary key's uniqueness rather than a read-then-write:
        two concurrent replays of the same captured proof both pass a read, and
        only one can insert.
        """

        session.add(
            DeviceProofNonce(
                nonce=nonce,
                device_id=device_id,
                seen_at=now,
                expires_at=now + NONCE_RETENTION,
            )
        )
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise InvalidDeviceProof("replayed_nonce") from exc

    async def _resolve_verifier(
        self, session: AsyncSession, device: Device, audit: AuditLogger
    ) -> bytes:
        """Resolve the stored public verifier through the SecretStore.

        The handle is all this module holds (SECRET-002). A locked or unavailable
        store makes this raise, which becomes an authentication *failure* — never
        a fallback that skips signature verification (12 §8, FAIL-012).
        """

        try:
            encoded = await self._store.get(
                session,
                device.credential_ref,
                SecretRequester.user(device.user_id),
                audit,
            )
        except SecretStoreError as exc:
            raise InvalidDeviceProof("credential_unresolvable") from exc

        try:
            return _b64d(encoded)
        except (ValueError, binascii.Error) as exc:
            raise InvalidDeviceProof("credential_corrupt") from exc

    # ── 03 §4.3 rotation ────────────────────────────────────────────────

    async def rotate_credential(
        self,
        session: AsyncSession,
        *,
        device: Device,
        audit: AuditLogger,
        public_key: str | None = None,
        key_proof: str | None = None,
    ) -> str | None:
        """Issue a new credential and invalidate the old one atomically.

        `[LOCKED]` (03 §4.3, AUTH-T6) no window where both work. There is exactly
        one stored verifier per device and `SecretStore.rotate` replaces it in
        place, so the old private key stops verifying the instant the new one
        starts — not "shortly after", and never both at once.

        Returns the new raw private key — again, exactly once — or `None` when
        the device rotated to a key pair it generated itself (`public_key`,
        signed over `rotation_message` by the new private key).
        """

        if (public_key is None) != (key_proof is None):
            raise InvalidDeviceProof("incomplete_public_key")
        private_key: Ed25519PrivateKey | None = None
        if public_key is not None and key_proof is not None:
            new_public = _verified_public_key(
                public_key,
                key_proof,
                rotation_message(device_id=device.device_id, public_key=public_key),
            )
        else:
            private_key = Ed25519PrivateKey.generate()
            new_public = _b64e(private_key.public_key().public_bytes_raw())
        await self._store.rotate(
            session,
            device.credential_ref,
            SecretRequester.user(device.user_id),
            audit,
            new_value=new_public,
        )

        # Live tokens were issued against the *previous* credential. Rotation is
        # usually a response to suspected exposure (03 §4.3), so anything the old
        # credential obtained is invalidated too.
        await self._repo.revoke_tokens_for_device(session, device_id=device.device_id)

        await audit.record(
            actor=AuditActor.USER,
            action=AuditAction.DEVICE_CREDENTIAL_ROTATED,
            resource=f"device:{device.device_id}",
            result=AuditResult.SUCCESS,
            user_id=device.user_id,
            device_id=device.device_id,
        )

        return _b64e(private_key.private_bytes_raw()) if private_key is not None else None

    # ── 03 §4.4 revocation ──────────────────────────────────────────────

    async def revoke(
        self,
        session: AsyncSession,
        *,
        device_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        audit: AuditLogger,
    ) -> Device | None:
        """Revoke a device (lost/stolen phone, SESSION-005).

        Only the owning user may revoke, and a device belonging to anyone else is
        reported as absent — a caller must not be able to discover another user's
        device ids by probing (04 §7).
        """

        device = await self._repo.get_device(session, device_id)
        if device is None or device.user_id != actor_user_id:
            return None

        if not device.revoked:
            device.revoked = True
            device.revoked_at = utcnow()

        # 03 §4.4: "invalidates the credential in the SecretStore".
        try:
            await self._store.delete(
                session, device.credential_ref, SecretRequester.user(device.user_id), audit
            )
        except SecretStoreError:
            # The device is already marked revoked, and `verify_proof` checks
            # that flag before it ever resolves a credential — so revocation is
            # effective even if the store is momentarily unavailable. Failing the
            # whole call here would leave the caller unable to revoke a stolen
            # phone, which is the opposite of fail-closed for this operation.
            pass

        await self._repo.revoke_tokens_for_device(session, device_id=device.device_id)
        await session.flush()

        await audit.record(
            actor=AuditActor.USER,
            action=AuditAction.DEVICE_REVOKED,
            resource=f"device:{device.device_id}",
            result=AuditResult.SUCCESS,
            user_id=actor_user_id,
            device_id=device.device_id,
        )
        return device
