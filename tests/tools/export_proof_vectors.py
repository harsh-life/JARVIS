"""`python -m tests.tools.export_proof_vectors [export|check]`.

Writes `shared/android/proof_vectors.json`: fixed-input Ed25519 signatures in
the exact formats the server verifies — the device proof (03 §4), the
registration proof-of-possession and the rotation proof (docs/23 §3). Ed25519
is deterministic, so the Android client must reproduce every value byte for
byte (android/contract ProofVectorTest); a format drift on either side fails
a test instead of every real login.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from server.auth.device import (
    PROOF_VERSION,
    _b64e,
    _canonical_message,
    registration_message,
    rotation_message,
)
from server.auth.step_up import attestation_message



PATH = Path(__file__).resolve().parents[2] / "shared" / "android" / "proof_vectors.json"

SEED = bytes(range(1, 33))
DEVICE_ID = "6f1c2d3e-4a5b-4c6d-8e7f-0123456789ab"
NONCE = "fixed-test-nonce_0123456789"
ISSUED_AT = 1790000000
BOOTSTRAP_TOKEN = "bootstrap-token-for-vectors-only"
# docs/23 §3 step-up: a fixed P-256 test key (never a real device's) and an
# RFC 6979 deterministic signature, so the file is reproducible.
STEP_UP_SCALAR = int.from_bytes(bytes(range(33, 65)), "big")
STEP_UP_CHALLENGE = "fixed-step-up-challenge_0123456789"


def vectors() -> dict:
    key = Ed25519PrivateKey.from_private_bytes(SEED)
    public = _b64e(key.public_key().public_bytes_raw())
    proof_sig = key.sign(_canonical_message(device_id=DEVICE_ID, nonce=NONCE, issued_at=ISSUED_AT))
    return {
        "seed_b64": _b64e(SEED),
        "public_key_b64": public,
        "device_id": DEVICE_ID,
        "nonce": NONCE,
        "issued_at": ISSUED_AT,
        "proof": f"{PROOF_VERSION}.{DEVICE_ID}.{NONCE}.{ISSUED_AT}.{_b64e(proof_sig)}",
        "bootstrap_token": BOOTSTRAP_TOKEN,
        "registration_signature": _b64e(
            key.sign(registration_message(bootstrap_token=BOOTSTRAP_TOKEN, public_key=public))
        ),
        "rotation_signature": _b64e(key.sign(rotation_message(device_id=DEVICE_ID, public_key=public))),
        "step_up": _step_up(),
    }


def _step_up() -> dict:
    key = ec.derive_private_key(STEP_UP_SCALAR, ec.SECP256R1())
    message = attestation_message(device_id=DEVICE_ID, challenge=STEP_UP_CHALLENGE)
    signature = key.sign(message, ec.ECDSA(hashes.SHA256(), deterministic_signing=True))
    return {
        "public_key_spki_b64": _b64e(key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)),
        "challenge": STEP_UP_CHALLENGE,
        "message": message.decode("ascii"),
        "signature_der_b64": _b64e(signature),
    }


def render() -> str:
    return json.dumps(vectors(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "check"
    if command == "export":
        PATH.write_text(render(), encoding="ascii")
        print(f"wrote {PATH}")
        return 0
    ok = PATH.exists() and PATH.read_text(encoding="ascii") == render()
    print("proof vectors up to date" if ok else "proof vectors stale — run export")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
