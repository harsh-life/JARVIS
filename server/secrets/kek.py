"""KEK provisioning and the unlock step (12 §3, OD-SEC-1).

`[LOCKED]` constraints from 12 §3 this module exists to hold:
- the KEK is **never** committed to git, baked into the APK, written to the
  application database, or logged;
- it is supplied **out-of-band at unlock time**;
- until it is supplied, secrets cannot be resolved (fail-closed);
- a crashed-and-restarted server does **not** auto-unlock from disk.

`[IMPL]` (OD-SEC-1) the provisioning method is env-injected-at-launch: the
config names an env *variable* (`secrets.kek_source: "env:HYPERMIND_KEK"`),
never a value. The variable holds a base64url-encoded 32-byte key.

Why a raw key rather than a passphrase: a passphrase needs a stored KDF salt,
which would put a second piece of the key hierarchy next to the ciphertext,
and it invites low-entropy operator input. Requiring a generated key keeps
the entropy guarantee unconditional. `python -m server.secrets.kek` prints a
fresh one. A passphrase+KDF variant remains open under OD-SEC-1 and would
not change any caller — only this module.
"""

from __future__ import annotations

import base64
import binascii
import os

from server.secrets.crypto import KEK_BYTES, generate_key
from server.secrets.errors import KEKUnavailable

ENV_PREFIX = "env:"


def encode_kek(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def generate_kek_value() -> str:
    """A fresh, correctly sized KEK, encoded for an env var."""

    return encode_kek(generate_key(KEK_BYTES))


def resolve_kek(kek_source: str, *, environ: dict[str, str] | None = None) -> bytes:
    """Obtain the KEK from its configured source.

    `kek_source` is the *source descriptor* from config (12 §3) — e.g.
    `env:HYPERMIND_KEK`. It is never the key.

    Every failure path raises `KEKUnavailable` rather than returning a
    default or a derived-from-nothing key: an unavailable KEK must leave the
    store locked (12 §8), not silently weaken it.
    """

    env = os.environ if environ is None else environ

    if not kek_source.startswith(ENV_PREFIX):
        raise KEKUnavailable(
            f"unsupported kek_source scheme: expected '{ENV_PREFIX}VAR_NAME' "
            f"(12 §3 / OD-SEC-1)"
        )

    var_name = kek_source[len(ENV_PREFIX) :]
    if not var_name:
        raise KEKUnavailable("kek_source names no environment variable")

    raw_value = env.get(var_name)
    if not raw_value:
        raise KEKUnavailable(
            f"the KEK environment variable {var_name} is unset or empty — the "
            f"SecretStore stays locked (12 §8). Generate one with "
            f"`python -m server.secrets.kek`."
        )

    try:
        key = base64.urlsafe_b64decode(raw_value)
    except (binascii.Error, ValueError) as exc:
        raise KEKUnavailable(
            f"the KEK in {var_name} is not valid base64url"
        ) from exc

    if len(key) != KEK_BYTES:
        raise KEKUnavailable(
            f"the KEK in {var_name} must decode to exactly {KEK_BYTES} bytes, "
            f"got {len(key)}"
        )

    return key


if __name__ == "__main__":  # pragma: no cover - operator helper
    print(generate_kek_value())
