"""The Google OAuth client secret: resolved per login, inside the auth boundary.

Google's token endpoint authenticates a **Web application** client by its
client secret even when the exchange also carries a PKCE verifier, so a
deployment using such a client names the secret with
`security.oidc.client_secret_ref` (`server/config/schema.py`). The config holds
only a reference, in one of the two forms every other credential uses:

* `env:NAME` — read from the server process's environment at the moment of
  the exchange (never cached here);
* `secretstore:<handle>` — resolved through `SecretStore.get` with the
  **server** requester and the login request's own transaction and audit
  writer. The handle must be a server-owned secret of class `oauth_token`:
  anything else is refused even when it exists, so this setting can never be
  pointed at a device credential or a user's model key and ship it to Google
  (the rule `server/composition/push.py` applies to its own credential).

The resolved value goes to exactly one place — the body of the token request
in `GoogleOIDCProvider.exchange_code` — and is held only for that call. It is
never logged, audited, cached, returned, or put in a URL, and nothing here
reaches the desktop companion or any browser. Every failure is an
`OIDCClientSecretUnavailable` whose `reason` names the kind of failure only.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.errors import OIDCClientSecretUnavailable
from server.secrets.audit_port import SecretAuditEvent
from server.secrets.errors import SecretStoreError
from server.secrets.requester import SecretRequester
from server.secrets.store import HANDLE_PREFIX, SecretStore
from server.security.audit import AuditLogger
from server.storage.models import SecretReference
from shared.schemas.enums import AuditResult, SecretClass, SecretOwnerScopeType

logger = logging.getLogger("hypermind.auth.client_secret")

ENV_PREFIX = "env:"
# The SecretStore class an OAuth client secret is stored under, and the only
# one this resolver accepts.
CLIENT_SECRET_CLASS = SecretClass.OAUTH_TOKEN


class ClientSecretConfigError(RuntimeError):
    """The configured reference cannot work (checked at startup, after the
    SecretStore unlocks). The message names the setting and the reference —
    which are configuration, not secrets — and never a value."""


@dataclass(frozen=True)
class ClientSecretSource:
    """Resolves `security.oidc.client_secret_ref`. Built once by the security
    composition root; holds the reference and the store, never the value."""

    secret_ref: str
    secret_store: SecretStore

    def __post_init__(self) -> None:
        if not (self.secret_ref.startswith(ENV_PREFIX) or self.secret_ref.startswith(HANDLE_PREFIX)):
            # Unreachable through config (SecretRef validates the form); kept so
            # a hand-built source cannot smuggle a literal value in.
            raise ValueError("client_secret_ref must be an env: or secretstore: reference")

    @property
    def kind(self) -> str:
        return "env" if self.secret_ref.startswith(ENV_PREFIX) else "secretstore"

    @property
    def _env_name(self) -> str:
        return self.secret_ref[len(ENV_PREFIX):]

    @property
    def _handle(self) -> str:
        # A SecretStore handle carries its own `secretstore:` prefix
        # (`server/secrets/store.py:new_handle`), so the configured reference
        # *is* the handle — exactly what `store` printed.
        return self.secret_ref

    async def resolve(self, session: AsyncSession, audit: AuditLogger) -> str:
        """The value, for one token exchange. Raises
        `OIDCClientSecretUnavailable` rather than returning anything empty."""

        if self.kind == "env":
            value = os.environ.get(self._env_name, "")
            if not value.strip():
                logger.error(
                    "OIDC client secret unavailable: security.oidc.client_secret_ref names "
                    "environment variable %s, which is unset or empty", self._env_name,
                )
                raise OIDCClientSecretUnavailable("client_secret_env_unset")
            return value

        handle = self._handle
        requester = SecretRequester.server()
        reference = await session.get(SecretReference, handle)
        if reference is not None and not _is_client_secret(reference):
            await audit.record_secret_event(SecretAuditEvent(
                action="secret.get", secret_ref=handle, requester=requester.describe(),
                result=AuditResult.BLOCKED, reason="not_an_oidc_client_secret",
            ))
            logger.error(
                "OIDC client secret unavailable: %s is not a server-owned %s secret",
                self.secret_ref, CLIENT_SECRET_CLASS.value,
            )
            raise OIDCClientSecretUnavailable("client_secret_wrong_class_or_scope")
        try:
            # Not found, revoked, store locked, integrity failure: the store
            # audits its own denials (by handle, never by value).
            value = await self.secret_store.get(session, handle, requester, audit)
        except SecretStoreError as exc:
            logger.error(
                "OIDC client secret unavailable: resolving %s failed (%s)",
                self.secret_ref, type(exc).__name__,
            )
            raise OIDCClientSecretUnavailable("client_secret_store_unavailable") from None
        if not value.strip():
            raise OIDCClientSecretUnavailable("client_secret_empty")
        return value

    async def verify(self, session: AsyncSession) -> None:
        """Startup check, without decrypting anything: the variable is set, or
        the handle exists, is unrevoked, server-owned and of the right class.
        Raises `ClientSecretConfigError` so a misconfigured server refuses to
        start instead of failing at the first sign-in."""

        if self.kind == "env":
            if not os.environ.get(self._env_name, "").strip():
                raise ClientSecretConfigError(
                    f"security.oidc.client_secret_ref is {self.secret_ref}, but that environment "
                    "variable is unset or empty"
                )
            return
        reference = await session.get(SecretReference, self._handle)
        if reference is None:
            raise ClientSecretConfigError(
                f"security.oidc.client_secret_ref is {self.secret_ref}, but no such SecretStore "
                "reference exists (store it with `python -m server.composition.oidc_client_secret store`)"
            )
        if reference.revoked_at is not None:
            raise ClientSecretConfigError(f"security.oidc.client_secret_ref {self.secret_ref} is revoked")
        if not _is_client_secret(reference):
            raise ClientSecretConfigError(
                f"security.oidc.client_secret_ref {self.secret_ref} is not a server-owned "
                f"{CLIENT_SECRET_CLASS.value} secret"
            )


def _is_client_secret(reference: SecretReference) -> bool:
    return (
        reference.class_ is CLIENT_SECRET_CLASS
        and reference.owner_scope_type is SecretOwnerScopeType.SERVER
    )
