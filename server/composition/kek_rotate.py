"""Operator command: replace the SecretStore's key-encryption key (12 §3).

    python -m server.composition.kek_rotate [--config PATH]

The current KEK comes from the variable `secrets.kek_source` names (as for
every other command); the new one from `HYPERMIND_NEW_KEK`. Neither is ever an
argument (argv is visible in `ps`), printed, logged or audited.

Every DEK row is re-sealed under the new KEK in one transaction. If the current
KEK does not open every row, nothing is written. On success the store opens
with the new KEK and not with the old one, which this command checks before it
returns 0. The audit trail gets one `secret.kek.rotate` row with no key in it.

Use deploy/rotate-kek.sh on a deployed host: it generates the new KEK into the
credentials directory, runs this, swaps the credential and restarts the
service (docs/DEPLOYMENT.md, "Rotating the KEK").
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from server.config import load_config
from server.gateway.security import build_security_core
from server.secrets.errors import KEKUnavailable, SecretIntegrityError, SecretStoreLocked
from server.secrets.kek import resolve_kek
from server.security.audit import AuditLogger
from server.storage import SQLAlchemyStorageBackend

NEW_KEK_SOURCE = "env:HYPERMIND_NEW_KEK"


async def _run(config_path: str | None) -> int:
    config = load_config(config_path)
    core = build_security_core(config)
    try:
        old_kek = resolve_kek(core.kek_source)
        new_kek = resolve_kek(NEW_KEK_SOURCE)
    except KEKUnavailable as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    storage = SQLAlchemyStorageBackend(config.database_url)
    try:
        async with storage.session() as session:
            audit = AuditLogger(session, request_id=uuid.uuid4())
            try:
                count = await core.secret_store.rewrap_kek(session, old_kek, new_kek, audit)
            except SecretIntegrityError:
                print("refused: the current KEK does not open this store; nothing was changed", file=sys.stderr)
                return 1
            except (SecretStoreLocked, ValueError) as exc:
                print(f"refused: {exc}; nothing was changed", file=sys.stderr)
                return 1
            await session.commit()

        # Prove the result from a fresh store object: the new KEK opens it and
        # the old one no longer does.
        async with storage.session() as session:
            fresh = build_security_core(config).secret_store
            await fresh.unlock(session, new_kek)
            fresh.lock()
            try:
                await fresh.unlock(session, old_kek)
            except SecretIntegrityError:
                pass
            else:
                print("NOT OK: the old KEK still opens the store", file=sys.stderr)
                return 1
            finally:
                fresh.lock()
    finally:
        await storage.dispose()
    print(f"re-sealed {count} data key(s) under the new KEK; the old KEK no longer opens this store")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m server.composition.kek_rotate")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.config))


if __name__ == "__main__":
    sys.exit(main())
