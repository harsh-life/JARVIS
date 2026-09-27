"""Push wake for sleeping phones (docs/23 §4) — off unless configured.

Two pieces:

* `FcmWakeSender` sends one Firebase Cloud Messaging HTTP v1 message to one
  registration token. The body is always `shared.schemas.push.fcm_wake_message`
  — there is no parameter through which anything else could be sent (ANDC-T9).
  Its OAuth2 assertion goes only to Google's fixed token endpoint; a service
  account naming any other `token_uri` is refused rather than followed.
* `DeviceWaker` decides whether a device is woken at all: only an enrolled,
  unrevoked device with its own push registration, only when its channel is
  not already up, and at most once per `min_interval_seconds`. Sending happens
  in the background; the caller learns only whether a wake is on its way.

A wake asks a phone to reconnect and authenticate like any other time. It
carries no operation and authorizes nothing; an operation that could not be
delivered has already failed (`device_unavailable`, never queued) — at most
the *task* waits for the channel and is re-authorized when it is back.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from enum import Enum
from typing import Awaitable, Callable

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import update

from server.storage import StorageBackend
from server.storage.models import Device
from shared.schemas.push import PushProvider, fcm_wake_message

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
FCM_SEND_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_ASSERTION_LIFETIME = 3600
_TOKEN_REFRESH_MARGIN = 120

CredentialProvider = Callable[[], Awaitable[str]]
# Records what became of a wake (audit) — injected at the composition root,
# since this layer cannot reach the audit logger (12 §2 import contract).
WakeRecorder = Callable[[uuid.UUID, uuid.UUID, "WakeOutcome"], Awaitable[None]]


class WakeOutcome(str, Enum):
    SENT = "sent"
    # The provider says the token is dead (uninstalled app, rotated token):
    # the binding is cleared so it is never used again.
    TOKEN_INVALID = "token_invalid"
    FAILED = "failed"


class WakeSender:
    """What `DeviceWaker` needs: deliver the fixed wake to one token."""

    async def send(self, token: str) -> WakeOutcome:  # pragma: no cover — protocol
        raise NotImplementedError


class PushCredentialError(Exception):
    """The sending credential is unusable. Carries no credential material."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _service_account(raw: str) -> tuple[str, rsa.RSAPrivateKey]:
    try:
        info = json.loads(raw)
    except ValueError:
        raise PushCredentialError("the push credential is not a service-account key (JSON)") from None
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise PushCredentialError("the push credential is not a service-account key")
    email = info.get("client_email")
    pem = info.get("private_key")
    if not isinstance(email, str) or not isinstance(pem, str):
        raise PushCredentialError("the service-account key lacks client_email/private_key")
    # The signed assertion is a bearer credential for this project's
    # messaging: it goes to Google's token endpoint and nowhere else.
    if info.get("token_uri", GOOGLE_TOKEN_URL) != GOOGLE_TOKEN_URL:
        raise PushCredentialError("the service-account key names an unexpected token_uri")
    try:
        key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except (ValueError, TypeError):
        raise PushCredentialError("the service-account private key does not load") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise PushCredentialError("the service-account key is not RSA")
    return email, key


def signed_assertion(email: str, key: rsa.RSAPrivateKey, *, now: int) -> str:
    """The OAuth2 JWT-bearer assertion (RFC 7523) for the FCM scope."""

    header = {"alg": "RS256", "typ": "JWT"}
    claims = {"iss": email, "scope": FCM_SCOPE, "aud": GOOGLE_TOKEN_URL, "iat": now,
              "exp": now + _ASSERTION_LIFETIME}
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":"), sort_keys=True).encode("utf-8")) for part in (header, claims)
    )
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(signature)}"


class FcmWakeSender(WakeSender):
    def __init__(
        self,
        *,
        project_id: str,
        credential: CredentialProvider,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._url = FCM_SEND_URL.format(project=project_id)
        self._credential = credential
        self._transport = transport
        self._clock = clock
        self._timeout = timeout_seconds
        self._access: tuple[str, float] | None = None
        self._lock = asyncio.Lock()

    async def send(self, token: str) -> WakeOutcome:
        body = fcm_wake_message(token)
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport,
                                         follow_redirects=False) as client:
                access = await self._access_token(client)
                response = await client.post(self._url, json=body, headers={"Authorization": f"Bearer {access}"})
        except (httpx.HTTPError, PushCredentialError) as exc:
            logger.warning("push wake not sent: %s", type(exc).__name__)
            return WakeOutcome.FAILED
        if response.status_code == 200:
            return WakeOutcome.SENT
        if response.status_code == 401:
            self._access = None
        if response.status_code == 404 or (response.status_code == 400 and _is_token_error(response)):
            return WakeOutcome.TOKEN_INVALID
        logger.warning("push wake rejected by the provider: HTTP %s", response.status_code)
        return WakeOutcome.FAILED

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        async with self._lock:
            now = self._clock()
            if self._access is not None and self._access[1] - _TOKEN_REFRESH_MARGIN > now:
                return self._access[0]
            try:
                raw = await self._credential()
            except Exception as exc:  # noqa: BLE001 — any resolution failure is "no credential"
                raise PushCredentialError("the push credential could not be resolved") from exc
            email, key = _service_account(raw)
            assertion = signed_assertion(email, key, now=int(now))
            response = await client.post(
                GOOGLE_TOKEN_URL,
                data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
            )
            if response.status_code != 200:
                raise PushCredentialError(f"token exchange failed: HTTP {response.status_code}")
            try:
                payload = response.json()
                access = payload["access_token"]
                lifetime = float(payload.get("expires_in", 3600))
            except (ValueError, KeyError, TypeError):
                raise PushCredentialError("token exchange returned an unexpected body") from None
            if not isinstance(access, str) or not access:
                raise PushCredentialError("token exchange returned no access token")
            self._access = (access, now + lifetime)
            return access


def _is_token_error(response: httpx.Response) -> bool:
    """FCM's 400 INVALID_ARGUMENT for our fixed message can only mean the
    token; UNREGISTERED is the explicit dead-token signal."""

    try:
        error = response.json().get("error", {})
    except ValueError:
        return False
    text = json.dumps(error)
    return "UNREGISTERED" in text or error.get("status") == "INVALID_ARGUMENT"


class DeviceWaker:
    def __init__(
        self,
        *,
        storage: StorageBackend,
        sender: WakeSender,
        is_connected: Callable[[uuid.UUID], Awaitable[bool]] | None = None,
        record: WakeRecorder | None = None,
        min_interval_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._storage = storage
        self._sender = sender
        self._is_connected = is_connected
        self._record = record
        self._min_interval = min_interval_seconds
        self._clock = clock
        self._last: dict[uuid.UUID, float] = {}
        self._owner: dict[uuid.UUID, uuid.UUID] = {}
        self._deciding: dict[tuple[uuid.UUID, uuid.UUID], asyncio.Future] = {}
        self._pending: set[asyncio.Task] = set()

    async def wake(self, device_id: uuid.UUID, *, user_id: uuid.UUID) -> bool:
        """Ask `device_id` — which must belong to `user_id` — to reconnect.
        True when a wake is on its way (just sent, or one within the
        coalescing interval); False when this device cannot be woken (no
        registration, revoked, unknown, another user's) or needs none."""

        if self._is_connected is not None and await self._is_connected(device_id):
            return False
        # One decision per device at a time: concurrent callers share it, so
        # a burst of failed sends to one offline phone makes one push.
        key = (device_id, user_id)
        inflight = self._deciding.get(key)
        if inflight is not None:
            return await asyncio.shield(inflight)
        decision: asyncio.Future = asyncio.get_running_loop().create_future()
        self._deciding[key] = decision
        try:
            woken = await self._decide(device_id, user_id)
        except BaseException:
            # The callers sharing this decision learn only "not woken".
            decision.set_result(False)
            raise
        else:
            decision.set_result(woken)
            return woken
        finally:
            del self._deciding[key]

    async def _decide(self, device_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        now = self._clock()
        last = self._last.get(device_id)
        if last is not None and now - last < self._min_interval and self._owner.get(device_id) == user_id:
            return True
        async with self._storage.session() as db:
            device = await db.get(Device, device_id)
            if (
                device is None
                or device.user_id != user_id
                or device.revoked
                or device.push_provider != PushProvider.FCM.value
                or not device.push_token
            ):
                return False
            token = device.push_token
        self._last[device_id] = now
        self._owner[device_id] = user_id
        task = asyncio.ensure_future(self._deliver(device_id, user_id, token))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return True

    async def drain(self) -> None:
        """Wait for in-flight sends (tests and shutdown)."""

        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    async def _deliver(self, device_id: uuid.UUID, user_id: uuid.UUID, token: str) -> None:
        try:
            outcome = await self._sender.send(token)
        except Exception:  # noqa: BLE001 — a failed wake is only a missed wake
            logger.warning("push wake to device %s failed", device_id, exc_info=True)
            outcome = WakeOutcome.FAILED
        if outcome is not WakeOutcome.SENT:
            self._last.pop(device_id, None)
        try:
            if outcome is WakeOutcome.TOKEN_INVALID:
                async with self._storage.session() as db:
                    # Compare-and-clear: a token the device re-registered
                    # meanwhile is not the dead one.
                    await db.execute(
                        update(Device)
                        .where(Device.device_id == device_id, Device.push_token == token)
                        .values(push_provider=None, push_token=None, push_token_registered_at=None)
                    )
                    await db.commit()
            if self._record is not None:
                await self._record(device_id, user_id, outcome)
        except Exception:  # noqa: BLE001
            logger.warning("recording the push wake for device %s failed", device_id, exc_info=True)


__all__ = [
    "DeviceWaker",
    "FcmWakeSender",
    "PushCredentialError",
    "WakeOutcome",
    "WakeRecorder",
    "WakeSender",
    "signed_assertion",
]
