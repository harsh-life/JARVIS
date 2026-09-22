"""The SecretStore — handle in, mediated resolution out (12_SECRETSTORE.md).

The three rules this module exists to enforce (12 §0):

1. **The value never leaves by any path except a scoped, authorized
   resolution** (SECRET-004).
2. **The agent never sees raw secret material** — it holds a handle
   (SECRET-002, INV-6).
3. **Master keys / superuser secrets are a separate principal** — application
   compromise does not yield them (SUPER-001).

Structure of the guarantee, in the order an attacker meets it:

- A handle (`secret_ref`) is all any caller above the boundary ever holds.
- `get` is the single resolution path, and it is mediated (§2 below) before
  any ciphertext is touched.
- The ciphertext lives in `secret_material`, encrypted under a DEK that is
  itself sealed under a KEK the application database never contains
  (`server/secrets/kek.py`). A stolen DB or backup therefore yields no
  plaintext (SS-T3).
- Until `unlock()` is called with that KEK, every resolution fails closed
  (SS-T8).

What this module deliberately does **not** claim (12 §4's honest bound):
RCE-proof isolation on a single process. A live compromised process with the
store already unlocked can read the DEK out of memory. That residual is
OD-A1's territory, measured in `docs/OD_A1_BR_T2.md` rather than asserted
away.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.secrets.audit_port import SecretAuditEvent, SecretAuditSink
from server.secrets.crypto import DEK_BYTES, aead_decrypt, aead_encrypt, generate_key, generate_token
from server.secrets.errors import (
    SecretDenied,
    SecretNotFound,
    SecretStoreLocked,
)
from server.secrets.requester import RequesterKind, SecretRequester
from server.storage.models import SecretMaterial, SecretReference, SecretStoreKey
from shared.schemas.enums import AuditResult, SecretClass, SecretOwnerScopeType

HANDLE_PREFIX = "secretstore:"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_handle() -> str:
    """An opaque handle (12 §1 "set returns a handle").

    The `secretstore:` prefix is the same vocabulary config uses for a
    `secret_ref` (15 §2 / server/config/schema.py), so a handle written into
    config and a handle minted here are interchangeable by construction.
    The rest carries no information about the secret's scope, class, or
    owner — a handle leaking tells an attacker nothing about what it points
    at.
    """

    return f"{HANDLE_PREFIX}{uuid.uuid4().hex}"


class SecretStore(ABC):
    """12 §1's interface. Replaceable (SECRET-001) — a future OS-keychain or
    KMS backend implements this same contract, and callers change nothing
    because they only ever hold handles."""

    @abstractmethod
    async def set(
        self,
        session: AsyncSession,
        *,
        owner_scope_type: SecretOwnerScopeType,
        owner_scope_id: str | None,
        secret_class: SecretClass,
        value: str,
        requester: SecretRequester,
        audit: SecretAuditSink,
    ) -> str: ...

    @abstractmethod
    async def get(
        self,
        session: AsyncSession,
        secret_ref: str,
        requester: SecretRequester,
        audit: SecretAuditSink,
    ) -> str: ...

    @abstractmethod
    async def delete(
        self,
        session: AsyncSession,
        secret_ref: str,
        requester: SecretRequester,
        audit: SecretAuditSink,
    ) -> None: ...

    @abstractmethod
    async def rotate(
        self,
        session: AsyncSession,
        secret_ref: str,
        requester: SecretRequester,
        audit: SecretAuditSink,
        *,
        new_value: str | None = None,
    ) -> str: ...


class EncryptedLocalSecretStore(SecretStore):
    """12 §3's MVP backend: encrypted-at-rest local store, external KEK.

    Lifecycle: construct → `unlock(session, kek)` (or `bootstrap` on first
    run) → resolutions succeed. A restart requires a fresh `unlock`; nothing
    on disk lets the store unlock itself (12 §3 "a crashed-and-restarted
    server does not auto-unlock from disk").
    """

    def __init__(self) -> None:
        self._dek: bytes | None = None
        self._key_id: str | None = None

    # ── unlock / bootstrap (12 §3) ──────────────────────────────────────

    @property
    def is_unlocked(self) -> bool:
        return self._dek is not None

    def lock(self) -> None:
        """Drop the unwrapped DEK from memory.

        Used on shutdown and by the OD-A1 experiment to measure the
        difference between a locked and an unlocked process.
        """

        self._dek = None
        self._key_id = None

    async def bootstrap(self, session: AsyncSession, kek: bytes) -> None:
        """Create the store's DEK on first run, sealed under the KEK.

        Idempotent: if a wrapped DEK already exists this is just an unlock,
        so re-running the documented bootstrap step (15 §3) can never
        silently replace the key that existing ciphertext was encrypted
        under.
        """

        existing = await self._load_active_key(session)
        if existing is not None:
            await self.unlock(session, kek)
            return

        dek = generate_key(DEK_BYTES)
        key_id = uuid.uuid4().hex
        nonce, wrapped = aead_encrypt(kek, dek, aad=key_id.encode("ascii"))
        session.add(
            SecretStoreKey(
                key_id=key_id,
                wrapped_dek=wrapped,
                wrap_nonce=nonce,
                created_at=_utcnow(),
            )
        )
        await session.flush()
        self._dek = dek
        self._key_id = key_id

    async def unlock(self, session: AsyncSession, kek: bytes) -> None:
        """Unwrap the DEK with the operator-supplied KEK.

        A wrong KEK raises `SecretIntegrityError` from the AEAD rather than
        producing a garbage key that would later corrupt every resolution
        (12 §3/§8).
        """

        row = await self._load_active_key(session)
        if row is None:
            raise SecretStoreLocked(
                "the SecretStore has never been bootstrapped — run the "
                "documented bootstrap step before starting the server (15 §3)"
            )
        self._dek = aead_decrypt(
            kek, row.wrap_nonce, row.wrapped_dek, aad=row.key_id.encode("ascii")
        )
        self._key_id = row.key_id

    async def _load_active_key(self, session: AsyncSession) -> SecretStoreKey | None:
        result = await session.execute(
            select(SecretStoreKey).where(SecretStoreKey.retired_at.is_(None))
        )
        return result.scalars().first()

    def _require_unlocked(self) -> bytes:
        if self._dek is None:
            raise SecretStoreLocked(
                "the SecretStore is locked; no secret can be resolved until it "
                "is unlocked with the KEK (12 §8 — the operation needing the "
                "secret is denied, never attempted without it)"
            )
        return self._dek

    # ── access mediation (12 §2) ────────────────────────────────────────

    @staticmethod
    def _authorize(reference: SecretReference, requester: SecretRequester) -> None:
        """The one place resolution authority is decided (12 §2).

        Every branch below ends in a denial or falls through to a return;
        there is no implicit allow, and an unrecognised scope or class is a
        denial rather than a default (fail-closed, FAIL-CORE-003).
        """

        # SECRET-002 / INV-6, unconditional and first: the agent cannot
        # resolve a secret, whatever the handle's scope or class. This is
        # the backstop behind the structural guarantee (the import-linter
        # contract stopping server.agent from importing this package).
        if requester.kind is RequesterKind.AGENT:
            raise SecretDenied("the agent may reference a secret only by handle")

        if reference.revoked_at is not None:
            raise SecretDenied("this secret reference is revoked")

        # SUPER-001 / SS-T4: a master key is resolvable only by the separate
        # superuser principal — never by a user, a tool, or the agent.
        if reference.class_ is SecretClass.MASTER_KEY and not requester.is_superuser:
            raise SecretDenied("master-key references are resolvable only by the superuser")

        # SS-T10: a tool receives only the secret its own configuration
        # declared. Checked before scope, because an undeclared handle is a
        # denial even when the tool's scope would otherwise match.
        if (
            requester.kind is RequesterKind.TOOL
            and reference.secret_ref not in requester.declared_secret_refs
        ):
            raise SecretDenied("this tool did not declare a need for this secret")

        if requester.is_superuser:
            return

        scope = reference.owner_scope_type

        if scope is SecretOwnerScopeType.SERVER:
            # 12 §7 (SECRET-003): the pilot's server-owned credentials
            # resolve only for server-owned operations — "never exposed to a
            # user, a user's tool, or the agent's context".
            if requester.kind is not RequesterKind.SERVER:
                raise SecretDenied("server-scoped secrets resolve only for server operations")
            return

        if scope is SecretOwnerScopeType.USER:
            if requester.user_id is None or str(requester.user_id) != reference.owner_scope_id:
                raise SecretDenied("this secret belongs to another user scope")
            return

        if scope is SecretOwnerScopeType.GRAPH:
            # GRAPH-009 / 12 §2: graph sharing never shares a secret. Graph
            # membership deliberately does *not* appear in this check — a
            # graph-scoped secret resolves only for deterministic
            # server-owned work in that graph, so no amount of membership
            # turns into a resolution.
            if requester.kind is not RequesterKind.SERVER:
                raise SecretDenied("graph-scoped secrets resolve only for server operations")
            if requester.graph_id is None or str(requester.graph_id) != reference.owner_scope_id:
                raise SecretDenied("this secret belongs to another graph scope")
            return

        raise SecretDenied("unrecognised secret scope")

    # ── the 12 §1 interface ─────────────────────────────────────────────

    async def set(
        self,
        session: AsyncSession,
        *,
        owner_scope_type: SecretOwnerScopeType,
        owner_scope_id: str | None,
        secret_class: SecretClass,
        value: str,
        requester: SecretRequester,
        audit: SecretAuditSink,
    ) -> str:
        """Store a value; return only a handle (12 §1).

        Beyond the doc's signature this also refuses an agent writer. 12 §2
        only spells out who may *resolve*, but letting the agent mint a
        secret reference would hand it a way to seed material it could later
        cause a boundary resolver to use — so the same absence applies to
        both directions.
        """

        if requester.kind is RequesterKind.AGENT:
            raise SecretDenied("the agent cannot store secret material")

        dek = self._require_unlocked()
        secret_ref = new_handle()
        now = _utcnow()

        nonce, ciphertext = aead_encrypt(
            dek, value.encode("utf-8"), aad=secret_ref.encode("utf-8")
        )

        session.add(
            SecretReference(
                secret_ref=secret_ref,
                owner_scope_type=owner_scope_type,
                owner_scope_id=owner_scope_id,
                class_=secret_class,
                created_at=now,
            )
        )
        session.add(
            SecretMaterial(
                secret_ref=secret_ref,
                key_id=self._key_id or "",
                nonce=nonce,
                ciphertext=ciphertext,
                updated_at=now,
            )
        )
        await session.flush()

        await audit.record_secret_event(
            SecretAuditEvent(
                action="secret.set",
                secret_ref=secret_ref,
                requester=requester.describe(),
                result=AuditResult.SUCCESS,
            )
        )
        return secret_ref

    async def get(
        self,
        session: AsyncSession,
        secret_ref: str,
        requester: SecretRequester,
        audit: SecretAuditSink,
    ) -> str:
        """The single resolution path (12 §1/§2).

        Called only by deterministic boundary code. Nothing in this method
        writes the resolved value anywhere — not to a log, not to an audit
        payload, not to a return-path structure that outlives the caller's
        use of it (SECRET-004).
        """

        dek = self._require_unlocked()

        reference = await session.get(SecretReference, secret_ref)
        if reference is None:
            await self._audit_denial(audit, "secret.get", secret_ref, requester, "not_found")
            raise SecretNotFound("no such secret reference")

        try:
            self._authorize(reference, requester)
        except SecretDenied as exc:
            await self._audit_denial(audit, "secret.get", secret_ref, requester, str(exc))
            raise

        material = await session.get(SecretMaterial, secret_ref)
        if material is None:
            # Metadata without material: a half-written or partly-deleted
            # secret. Fail closed rather than guessing (12 §3 crash
            # recovery — "never a corrupt half-secret").
            await self._audit_denial(audit, "secret.get", secret_ref, requester, "material_missing")
            raise SecretNotFound("secret material is absent for this reference")

        plaintext = aead_decrypt(
            dek, material.nonce, material.ciphertext, aad=secret_ref.encode("utf-8")
        )

        await audit.record_secret_event(
            SecretAuditEvent(
                action="secret.get",
                secret_ref=secret_ref,
                requester=requester.describe(),
                result=AuditResult.SUCCESS,
            )
        )
        return plaintext.decode("utf-8")

    async def delete(
        self,
        session: AsyncSession,
        secret_ref: str,
        requester: SecretRequester,
        audit: SecretAuditSink,
    ) -> None:
        """Revoke immediately (12 §5): the next resolution fails.

        The material row is removed and the reference is marked revoked. The
        reference row survives so the revocation remains auditable and a
        replayed handle is denied rather than looking merely absent.
        """

        reference = await session.get(SecretReference, secret_ref)
        if reference is None:
            raise SecretNotFound("no such secret reference")

        try:
            self._authorize(reference, requester)
        except SecretDenied as exc:
            await self._audit_denial(audit, "secret.delete", secret_ref, requester, str(exc))
            raise

        material = await session.get(SecretMaterial, secret_ref)
        if material is not None:
            await session.delete(material)
        reference.revoked_at = _utcnow()
        await session.flush()

        await audit.record_secret_event(
            SecretAuditEvent(
                action="secret.delete",
                secret_ref=secret_ref,
                requester=requester.describe(),
                result=AuditResult.SUCCESS,
            )
        )

    async def rotate(
        self,
        session: AsyncSession,
        secret_ref: str,
        requester: SecretRequester,
        audit: SecretAuditSink,
        *,
        new_value: str | None = None,
    ) -> str:
        """Replace the value atomically (12 §5) — no dual-valid window.

        The handle is stable, which is what makes 12 §5's "callers holding
        the handle transparently resolve the new value" true. There is
        exactly one ciphertext row per handle and it is overwritten in place
        inside the caller's transaction, so no moment exists at which both
        the old and the new value would verify.
        """

        dek = self._require_unlocked()

        reference = await session.get(SecretReference, secret_ref)
        if reference is None:
            raise SecretNotFound("no such secret reference")

        try:
            self._authorize(reference, requester)
        except SecretDenied as exc:
            await self._audit_denial(audit, "secret.rotate", secret_ref, requester, str(exc))
            raise

        material = await session.get(SecretMaterial, secret_ref)
        if material is None:
            raise SecretNotFound("secret material is absent for this reference")

        value = new_value if new_value is not None else generate_token()
        nonce, ciphertext = aead_encrypt(dek, value.encode("utf-8"), aad=secret_ref.encode("utf-8"))
        now = _utcnow()

        material.nonce = nonce
        material.ciphertext = ciphertext
        material.key_id = self._key_id or ""
        material.updated_at = now
        reference.rotated_at = now
        await session.flush()

        await audit.record_secret_event(
            SecretAuditEvent(
                action="secret.rotate",
                secret_ref=secret_ref,
                requester=requester.describe(),
                result=AuditResult.SUCCESS,
            )
        )
        return secret_ref

    # ── observability (DASH-005: existence + metadata, never the value) ──

    async def describe(self, session: AsyncSession, secret_ref: str) -> SecretReference | None:
        """Metadata only.

        This is what a dashboard is allowed to see (DASH-005: "shows a secret
        *exists* + metadata, never its value"). It returns the metadata row,
        which by foundation's own assertion has no column capable of holding
        a value.
        """

        return await session.get(SecretReference, secret_ref)

    async def _audit_denial(
        self,
        audit: SecretAuditSink,
        action: str,
        secret_ref: str,
        requester: SecretRequester,
        reason: str,
    ) -> None:
        await audit.record_secret_event(
            SecretAuditEvent(
                action=action,
                secret_ref=secret_ref,
                requester=requester.describe(),
                result=AuditResult.BLOCKED,
                reason=reason,
            )
        )
