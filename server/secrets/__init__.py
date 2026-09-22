"""SecretStore — 12_SECRETSTORE.md.

`[LOCKED]` (16 §3, the most important module-boundary rule) this package
"exposes only the handle-based interface + the resolution-at-boundary
function — it does **not** export a 'give me the raw value' function to
general callers." Concretely:

- the only resolution entry point is `SecretStore.get`, and it takes a
  `SecretRequester` it then mediates on (12 §2);
- there is no module-level convenience function that resolves a handle
  without a requester, because such a function is exactly what a future
  caller would reach for "for convenience" and it would silently void
  SECRET-002;
- every lifecycle method requires an audit sink as a positional argument, so
  an un-audited secret operation cannot happen by omission (12 §5);
- `server.agent` cannot import this package at all — enforced in CI by
  pyproject's import-linter contracts (REPO-T1), with
  `RequesterKind.AGENT`'s unconditional denial as the backstop behind it.
"""

from server.secrets.audit_port import (
    NullSecretAuditSink,
    SecretAuditEvent,
    SecretAuditSink,
)
from server.secrets.errors import (
    KEKUnavailable,
    SecretDenied,
    SecretIntegrityError,
    SecretNotFound,
    SecretStoreError,
    SecretStoreLocked,
)
from server.secrets.kek import generate_kek_value, resolve_kek
from server.secrets.requester import (
    RequesterKind,
    SecretRequester,
    SuperuserGrant,
)
from server.secrets.store import EncryptedLocalSecretStore, SecretStore, new_handle

__all__ = [
    "EncryptedLocalSecretStore",
    "KEKUnavailable",
    "NullSecretAuditSink",
    "RequesterKind",
    "SecretAuditEvent",
    "SecretAuditSink",
    "SecretDenied",
    "SecretIntegrityError",
    "SecretNotFound",
    "SecretRequester",
    "SecretStore",
    "SecretStoreError",
    "SecretStoreLocked",
    "SuperuserGrant",
    "generate_kek_value",
    "new_handle",
    "resolve_kek",
]
