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

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from server.auth.device import (
    PROOF_VERSION,
    _b64e,
    _canonical_message,
    registration_message,
    rotation_message,
)

PATH = Path(__file__).resolve().parents[2] / "shared" / "android" / "proof_vectors.json"

SEED = bytes(range(1, 33))
DEVICE_ID = "6f1c2d3e-4a5b-4c6d-8e7f-0123456789ab"
NONCE = "fixed-test-nonce_0123456789"
ISSUED_AT = 1790000000
BOOTSTRAP_TOKEN = "bootstrap-token-for-vectors-only"


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
