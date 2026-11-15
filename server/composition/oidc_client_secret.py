"""Operator commands for the Google OAuth client secret (docs/GOOGLE_OIDC_SETUP.md).

    python -m server.composition.oidc_client_secret store  [--config PATH]
        Read the secret (hidden prompt on a terminal, else one line of stdin),
        store it in the SecretStore as a server-owned `oauth_token`, and print
        only its handle — the value to put in `security.oidc.client_secret_ref`.

    python -m server.composition.oidc_client_secret rotate --handle H [--config PATH]
        Replace the value under an existing handle (same handle, no config
        change; the next sign-in uses the new value). Refused unless the handle
        is a server-owned `oauth_token`.

    python -m server.composition.oidc_client_secret check  [--config PATH] [--live]
        The startup check: is the configured `client_secret_ref` usable?
        With --live, also ask Google whether it accepts this client ID and
        secret: one token request with a deliberately invalid code (no real
        code is used and no token can be issued). `invalid_client` means the
        stored secret does not belong to this client — e.g. a deleted client's.

Each command needs the KEK in the environment variable `secrets.kek_source`
names, and an already-bootstrapped SecretStore (docs/RUNNING_SECURITY_CORE.md
§4). The secret is never printed, logged or audited; audit rows carry the
handle and the `server` requester only.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
import uuid

from server.auth.client_secret import CLIENT_SECRET_CLASS, ClientSecretConfigError
from server.config import load_config
from server.gateway.security import (
    build_security_core,
    probe_oidc_client_authentication,
    unlock_secret_store,
    verify_oidc_client_secret,
)
from server.secrets.requester import SecretRequester
from server.security.audit import AuditLogger
from server.storage import SQLAlchemyStorageBackend
from server.storage.models import SecretReference
from shared.schemas.enums import SecretOwnerScopeType

MAX_SECRET_CHARS = 512


def _read_secret() -> str:
    if sys.stdin.isatty():
        value = getpass.getpass("Google OAuth client secret (input hidden): ")
    else:
        value = sys.stdin.readline()
    value = value.strip()
    if not value or len(value) > MAX_SECRET_CHARS or any(c.isspace() for c in value):
        # Never the value, its length or a fragment of it.
        raise SystemExit("refused: that is not a usable client secret")
    return value


_LIVE_VERDICTS = {
    "authenticated": (0, "ok: Google accepts the client secret for client_id {cid}"),
    "rejected": (1, "NOT OK: Google rejected the client secret for client_id {cid} (invalid_client). The stored "
                    "secret does not belong to this client (for example, a deleted client's secret). Replace it "
                    "under the same handle: rotate --handle {ref}"),
    "secret_missing": (1, "NOT OK: Google requires a client secret for client_id {cid}; set "
                          "security.oidc.client_secret_ref"),
    "inconclusive": (2, "INCONCLUSIVE: Google's answer did not show whether client_id {cid} authenticated"),
    "unsupported": (2, "INCONCLUSIVE: the configured provider has no live client check"),
}


async def _run(command: str, config_path: str | None, handle: str | None, live: bool = False) -> int:
    config = load_config(config_path)
    storage = SQLAlchemyStorageBackend(config.database_url)
    core = build_security_core(config)
    try:
        await unlock_secret_store(core, storage)
        if command == "check":
            if config.security.oidc.client_secret_ref is None:
                print("security.oidc.client_secret_ref is not set: no client secret is sent (public client)")
                return 0
            try:
                await verify_oidc_client_secret(core, storage)
            except ClientSecretConfigError as exc:
                print(f"NOT OK: {exc}", file=sys.stderr)
                return 1
            print(f"ok: {config.security.oidc.client_secret_ref} is usable")
            if not live:
                return 0
            verdict = await probe_oidc_client_authentication(core, storage)
            code, message = _LIVE_VERDICTS[verdict]
            print(message.format(cid=config.security.oidc.client_id, ref=config.security.oidc.client_secret_ref),
                  file=sys.stderr if code else sys.stdout)
            return code

        value = _read_secret()
        requester = SecretRequester.server()
        async with storage.session() as session:
            audit = AuditLogger(session, request_id=uuid.uuid4())
            if command == "store":
                new = await core.secret_store.set(
                    session,
                    owner_scope_type=SecretOwnerScopeType.SERVER,
                    owner_scope_id=None,
                    secret_class=CLIENT_SECRET_CLASS,
                    value=value,
                    requester=requester,
                    audit=audit,
                )
                await session.commit()
                print(new)
                print(f'set security.oidc.client_secret_ref: "{new}"', file=sys.stderr)
                return 0

            assert handle is not None
            reference = await session.get(SecretReference, handle)
            if (
                reference is None
                or reference.revoked_at is not None
                or reference.class_ is not CLIENT_SECRET_CLASS
                or reference.owner_scope_type is not SecretOwnerScopeType.SERVER
            ):
                print(f"refused: {handle} is not an active server-owned {CLIENT_SECRET_CLASS.value} secret",
                      file=sys.stderr)
                return 1
            await core.secret_store.rotate(session, handle, requester, audit, new_value=value)
            await session.commit()
            print(f"rotated {handle}; the next sign-in uses the new value")
            return 0
    finally:
        await storage.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m server.composition.oidc_client_secret")
    parser.add_argument("command", choices=["store", "rotate", "check"])
    parser.add_argument("--config", default=None)
    parser.add_argument("--handle", default=None, help="the existing handle (rotate only)")
    parser.add_argument("--live", action="store_true",
                        help="check only: also ask Google whether it accepts this client ID and secret")
    args = parser.parse_args(argv)
    if args.command == "rotate" and not args.handle:
        parser.error("rotate needs --handle")
    if args.live and args.command != "check":
        parser.error("--live applies to check only")
    return asyncio.run(_run(args.command, args.config, args.handle, args.live))


if __name__ == "__main__":
    sys.exit(main())
