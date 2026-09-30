"""Canonical hashing of a compiled spec (docs/29 §9.5, §9.6 rule 8).

The hash covers every field of the spec except `created_at` and the hash
itself, serialized as sorted-key, whitespace-free JSON — the same
canonicalization `server/agent/recovery.py::operation_key` uses, so key order
and formatting never make two identical specs differ. A stored spec whose
hash no longer recomputes has been altered outside the compiler.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

_EXCLUDED = frozenset({"created_at", "spec_hash"})


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def spec_hash_of(fields: Mapping[str, Any]) -> str:
    """`fields` is a spec's JSON-mode dump (or the dict the compiler is about
    to build a spec from)."""

    payload = {k: v for k, v in fields.items() if k not in _EXCLUDED}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


__all__ = ["canonical_json", "spec_hash_of"]
