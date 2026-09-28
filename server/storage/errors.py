"""Which storage failures are transient ("not now", never "not ever").

SQLite's single writer answers "database is locked" while another request holds
the write lock. PostgreSQL (the pilot's runtime store, H-1) reports conflicts by
SQLSTATE. In both cases the transaction was rolled back, so nothing it did
persisted and the same work can be retried unchanged.
"""

from __future__ import annotations

from sqlalchemy.exc import DBAPIError

_TRANSIENT_SQLSTATES = frozenset({
    "40001",  # serialization_failure
    "40P01",  # deadlock_detected
    "55P03",  # lock_not_available (lock_timeout)
    "53300",  # too_many_connections
    "57P03",  # cannot_connect_now
})


def sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    for candidate in (orig, getattr(orig, "__cause__", None)):
        state = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if isinstance(state, str):
            return state
    return None


def is_transient_store_error(exc: BaseException) -> bool:
    if not isinstance(exc, DBAPIError):
        return False
    return "database is locked" in str(getattr(exc, "orig", exc)) or sqlstate(exc) in _TRANSIENT_SQLSTATES


__all__ = ["is_transient_store_error", "sqlstate"]
