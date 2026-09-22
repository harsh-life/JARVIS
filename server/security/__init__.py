"""Security primitives: the audit path and superuser separation.

Scope note (16 §5): this package holds *cross-cutting security primitives*,
not the authorization decision. The five-dimension engine lives in
`server/graph/authorization.py` (04) and the capability/risk/floor policy in
`server/capabilities/` (07), deliberately kept apart from each other and from
this module — there is no single global "SecurityManager" that owns
everything, because such a class is precisely where a boundary quietly
disappears.
"""

from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.security.superuser import (
    SUPERUSER_TOKEN_ENV,
    SuperuserAuthenticationFailed,
    SuperuserNotConfigured,
    authenticate_superuser,
)

__all__ = [
    "SUPERUSER_TOKEN_ENV",
    "AuditAction",
    "AuditLogger",
    "SuperuserAuthenticationFailed",
    "SuperuserNotConfigured",
    "authenticate_superuser",
]
