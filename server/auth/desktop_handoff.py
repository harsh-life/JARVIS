"""The desktop companion's login handoff (OD-DESK-1).

The companion signs in through the **system browser**, never an embedded
webview, and is handed its login back through a loopback listener
(RFC 8252 §7.3). What crosses the browser must therefore be worthless on its
own, so the flow carries three things the browser never sees together:

```
 companion                         server                              browser
 ─────────                         ──────                              ───────
 verifier, desktop_state (random)
 GET /auth/oidc/start ?desktop_state&desktop_challenge=S256(verifier)&desktop_port
                                   store them on the login state ─────▶ Google consent
                                   ◀──────────────────────────── /auth/oidc/callback
                                   every 03 §2.3 check, then a handoff code
                                   302 http://127.0.0.1:<port>/jarvis/callback?code&state
 loopback listener: state matches?
 POST /auth/desktop/handoff {code, code_verifier}
                                   S256(verifier) == challenge? single use? unexpired?
                                   → bootstrap token bound to `desktop`
 POST /devices {platform: desktop, public_key, key_proof}   (unchanged 03 §3)
 POST /sessions/token {device_credential: proof}             (unchanged 03 §5)
```

* **No bearer credential in a URL.** The browser carries only the handoff
  code, which nothing accepts without the verifier; the verifier and the
  bootstrap token travel in request and response bodies between the companion
  and the server only.
* **Single use, short TTL.** The code is stored as a hash and spent by a
  conditional UPDATE, exactly as the bootstrap token and the login state are.
* **Login injection** (someone else's login delivered to this companion) fails
  twice: the companion checks its own `desktop_state`, and a code issued for
  another login was bound to another verifier.
* **The loopback port** is the only part of the redirect the companion
  chooses, and the host is fixed to `127.0.0.1` here — the callback cannot be
  turned into an open redirect.

Identity is still derived only from the validated id_token (PHONE-003): the
handoff row's `user_id` came from `OIDCLoginFlow.complete`, never from the
companion.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.bootstrap import issue_bootstrap_token
from server.auth.errors import InvalidDesktopHandoff
from server.secrets.crypto import generate_token, hash_token
from server.storage.models import DesktopHandoff
from shared.schemas.enums import DevicePlatform

DESKTOP_HANDOFF_TTL = timedelta(seconds=120)

# RFC 7636 §4.1: a verifier is 43–128 unreserved characters. The challenge is
# always S256, base64url without padding of a SHA-256 digest: 43 characters.
VERIFIER_PATTERN = r"^[A-Za-z0-9._~-]{43,128}$"
CHALLENGE_PATTERN = r"^[A-Za-z0-9_-]{43}$"
# The companion's own nonce, like the Android `app_state`.
DESKTOP_STATE_PATTERN = r"^[A-Za-z0-9_-]{32,128}$"
# Loopback ports the companion may ask to be returned to: never a privileged one.
DESKTOP_PORT_MIN = 1024
DESKTOP_PORT_MAX = 65535
# The loopback address and path the callback returns to. The host is fixed
# here, never taken from a request.
LOOPBACK_HOST = "127.0.0.1"
LOOPBACK_PATH = "/jarvis/callback"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def s256_challenge(verifier: str) -> str:
    """RFC 7636 §4.2's S256 transform."""

    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def loopback_redirect(*, port: int, code: str, desktop_state: str) -> str:
    """The callback's redirect to the companion's loopback listener."""

    if not DESKTOP_PORT_MIN <= port <= DESKTOP_PORT_MAX:
        raise ValueError("loopback port out of range")
    # Both values are URL-safe by construction (`generate_token` and the
    # validated state pattern), so no encoding can change their meaning.
    return f"http://{LOOPBACK_HOST}:{port}{LOOPBACK_PATH}?code={code}&state={desktop_state}"


async def issue_desktop_handoff(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    code_challenge: str,
    ttl: timedelta = DESKTOP_HANDOFF_TTL,
) -> str:
    """Mint the one-time handoff code for a completed desktop login. Only its
    hash is stored; the returned string is the only copy."""

    raw = generate_token()
    now = _utcnow()
    session.add(
        DesktopHandoff(
            code_hash=hash_token(raw),
            user_id=user_id,
            code_challenge=code_challenge,
            created_at=now,
            expires_at=now + ttl,
        )
    )
    await session.flush()
    return raw


async def redeem_desktop_handoff(
    session: AsyncSession, *, code: str, code_verifier: str
) -> tuple[uuid.UUID, str]:
    """Spend a handoff code presented with its verifier; return the user and a
    fresh bootstrap token bound to the `desktop` platform.

    Order: look up → check expiry → check the verifier → spend. The verifier is
    checked *before* the spend so that someone holding only an intercepted code
    cannot burn the companion's login with a guess; the spend is still a
    conditional UPDATE, so two concurrent redemptions cannot both succeed.
    """

    if not code or not code_verifier:
        raise InvalidDesktopHandoff("absent")
    row = await session.get(DesktopHandoff, hash_token(code))
    if row is None:
        raise InvalidDesktopHandoff("unknown")
    if row.used_at is not None:
        raise InvalidDesktopHandoff("replayed")
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= _utcnow():
        raise InvalidDesktopHandoff("expired")
    if not hmac.compare_digest(s256_challenge(code_verifier), row.code_challenge):
        raise InvalidDesktopHandoff("verifier_mismatch")

    result = await session.execute(
        update(DesktopHandoff)
        .where(DesktopHandoff.code_hash == row.code_hash, DesktopHandoff.used_at.is_(None))
        .values(used_at=_utcnow())
    )
    await session.flush()
    if result.rowcount != 1:
        raise InvalidDesktopHandoff("replayed")

    bootstrap = await issue_bootstrap_token(
        session, user_id=row.user_id, platform=DevicePlatform.DESKTOP
    )
    return row.user_id, bootstrap
