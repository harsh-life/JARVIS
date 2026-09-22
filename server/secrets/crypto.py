"""Cryptographic primitives for the SecretStore (12 §3).

`[IMPL]` choices, documented per 12 §3's "exact primitives [IMPL]" while the
guarantees it locks are all preserved:

- **AEAD = AES-256-GCM.** 12 §3 requires authenticated encryption — "a
  tampered ciphertext must fail, not silently decrypt to garbage". AES-GCM
  is the doc's own named example and is hardware-accelerated everywhere
  Track B runs.
- **Nonces are 96-bit, CSPRNG, never reused.** A fresh nonce is drawn per
  encryption (including per rotation), so no key/nonce pair repeats.
- **The handle is the AAD.** Every secret's ciphertext is bound to its own
  `secret_ref`. Moving a ciphertext row onto a different handle — the
  obvious attack once you can write the DB but not read the KEK — makes
  decryption fail authentication rather than silently yielding another
  scope's value.
- **All key material is CSPRNG** (`os.urandom` via `cryptography`), never
  derived from a predictable source (12 §3 "no weak/predictable key
  generation").

This module holds no state and no keys; it is pure functions over bytes.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets as _stdlib_secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from server.secrets.errors import SecretIntegrityError

DEK_BYTES = 32  # AES-256
KEK_BYTES = 32
NONCE_BYTES = 12  # 96-bit, the AES-GCM standard nonce size


def generate_key(length: int = DEK_BYTES) -> bytes:
    """CSPRNG key material (12 §3 "key generation")."""

    return os.urandom(length)


def generate_token(nbytes: int = 32) -> str:
    """A high-entropy, URL-safe opaque token.

    Used for every credential-shaped value this branch issues (access
    tokens, bootstrap tokens, state/nonce, confirmation tokens). 32 bytes =
    256 bits, so the stored SHA-256 hash is not brute-forceable and a DB
    leak yields nothing usable.
    """

    return _stdlib_secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """The stored form of an opaque token.

    A plain SHA-256 is correct *here* and a password hash would not be:
    these tokens are full-entropy random values, not user-chosen secrets, so
    there is no dictionary to stretch against — and the lookup is on the
    hot path of every authenticated request.
    """

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def aead_encrypt(key: bytes, plaintext: bytes, *, aad: bytes) -> tuple[bytes, bytes]:
    """Encrypt under AES-256-GCM. Returns `(nonce, ciphertext)`."""

    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    return nonce, ciphertext


def aead_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, *, aad: bytes) -> bytes:
    """Decrypt and authenticate.

    `[LOCKED]` (12 §8) an authentication failure is a hard error — the
    caller never receives partial or unauthenticated plaintext.
    """

    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise SecretIntegrityError(
            "secret material failed authentication — refusing to proceed with "
            "suspect material"
        ) from exc
