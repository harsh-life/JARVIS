"""KEK rotation (12 §3): re-sealing the SecretStore's data keys under a new KEK.

The remedy for a KEK that may have been exposed (deploy/rotate-kek.sh,
docs/DEPLOYMENT.md "Rotating the KEK"). What must hold:

* after rotation the store opens with the new KEK and every secret resolves
  to the same value under the same handle — no secret is re-encrypted;
* the old KEK no longer opens it;
* a wrong "current" KEK changes nothing (no store split across two KEKs);
* the audit trail records the rotation and never a key.
"""

from __future__ import annotations

import base64
import uuid

import pytest
from sqlalchemy import select

from server.composition import kek_rotate
from server.secrets.errors import SecretIntegrityError, SecretStoreLocked
from server.secrets.requester import SecretRequester
from server.secrets.store import EncryptedLocalSecretStore
from server.storage.models import AuditEvent, SecretMaterial, SecretStoreKey
from shared.schemas.enums import SecretClass, SecretOwnerScopeType
from tests.security_core.helpers import database_contains
from tests.dbsupport import database_url_for
from tests.support import TEST_KEK_ENV_VAR, make_test_config, make_test_kek

pytestmark = pytest.mark.asyncio

TEST_SECRET_VALUE = "TEST-ONLY-kek-rotation-value-5d1e"


def _raw(value: str) -> bytes:
    return base64.urlsafe_b64decode(value)


def _write_config(path, database_url: str) -> str:
    import yaml

    config = make_test_config(database_url=database_url)
    path.write_text(yaml.safe_dump(config.model_dump(mode="json", exclude_none=True)))
    return str(path)


async def _store_one(store, db, audit) -> str:
    return await store.set(
        db,
        owner_scope_type=SecretOwnerScopeType.SERVER,
        owner_scope_id=None,
        secret_class=SecretClass.OAUTH_TOKEN,
        value=TEST_SECRET_VALUE,
        requester=SecretRequester.server(),
        audit=audit,
    )


async def test_rotation_reseals_under_the_new_kek_and_secrets_still_resolve(store, db, audit, kek_value):
    handle = await _store_one(store, db, audit)
    material_before = (await db.get(SecretMaterial, handle)).ciphertext
    new_value = make_test_kek()

    assert await store.rewrap_kek(db, _raw(kek_value), _raw(new_value), audit) == 1
    await db.flush()

    # Same handle, same ciphertext (nothing re-encrypted), new KEK opens it.
    assert (await db.get(SecretMaterial, handle)).ciphertext == material_before
    fresh = EncryptedLocalSecretStore()
    await fresh.unlock(db, _raw(new_value))
    assert await fresh.get(db, handle, SecretRequester.server(), audit) == TEST_SECRET_VALUE


async def test_after_rotation_the_old_kek_no_longer_opens_the_store(store, db, audit, kek_value):
    await _store_one(store, db, audit)
    await store.rewrap_kek(db, _raw(kek_value), _raw(make_test_kek()), audit)
    await db.flush()

    with pytest.raises(SecretIntegrityError):
        await EncryptedLocalSecretStore().unlock(db, _raw(kek_value))


async def test_a_wrong_current_kek_changes_nothing(store, db, audit, kek_value):
    await _store_one(store, db, audit)
    before = [(r.key_id, r.wrap_nonce, r.wrapped_dek) for r in (await db.execute(select(SecretStoreKey))).scalars()]

    with pytest.raises(SecretIntegrityError):
        await store.rewrap_kek(db, _raw(make_test_kek()), _raw(make_test_kek()), audit)

    after = [(r.key_id, r.wrap_nonce, r.wrapped_dek) for r in (await db.execute(select(SecretStoreKey))).scalars()]
    assert after == before
    await EncryptedLocalSecretStore().unlock(db, _raw(kek_value))   # the real KEK still works


async def test_every_key_row_is_resealed_including_retired_ones(store, db, audit, kek_value):
    # A retired DEK may still be referenced by older material; it must move too,
    # or the store would be split across two KEKs.
    from server.secrets.crypto import aead_encrypt, generate_key
    import datetime

    key_id = uuid.uuid4().hex
    nonce, wrapped = aead_encrypt(_raw(kek_value), generate_key(32), aad=key_id.encode("ascii"))
    now = datetime.datetime.now(datetime.timezone.utc)
    db.add(SecretStoreKey(key_id=key_id, wrapped_dek=wrapped, wrap_nonce=nonce, created_at=now, retired_at=now))
    await db.flush()
    new_value = make_test_kek()

    assert await store.rewrap_kek(db, _raw(kek_value), _raw(new_value), audit) == 2
    from server.secrets.crypto import aead_decrypt
    retired = await db.get(SecretStoreKey, key_id)
    aead_decrypt(_raw(new_value), retired.wrap_nonce, retired.wrapped_dek, aad=key_id.encode("ascii"))


async def test_the_same_kek_is_refused(store, db, audit, kek_value):
    with pytest.raises(ValueError):
        await store.rewrap_kek(db, _raw(kek_value), _raw(kek_value), audit)


async def test_an_unbootstrapped_store_is_refused(db, audit):
    with pytest.raises(SecretStoreLocked):
        await EncryptedLocalSecretStore().rewrap_kek(db, _raw(make_test_kek()), _raw(make_test_kek()), audit)


async def test_rotation_is_audited_without_any_key(store, db, audit, kek_value):
    new_value = make_test_kek()
    await store.rewrap_kek(db, _raw(kek_value), _raw(new_value), audit)
    await db.flush()

    rows = (await db.execute(select(AuditEvent).where(AuditEvent.action == "secret.kek.rotate"))).scalars().all()
    assert len(rows) == 1 and rows[0].resource == "secretstore:keys"
    assert not await database_contains(db, kek_value)
    assert not await database_contains(db, new_value)


# ── the operator command ──────────────────────────────────────────────────


async def test_the_cli_rotates_and_proves_old_out_new_in(tmp_path, monkeypatch, capsys):
    from server.storage import SQLAlchemyStorageBackend

    url = database_url_for(tmp_path / "kek.db")
    backend = SQLAlchemyStorageBackend(url)
    await backend.init_models()
    old_value, new_value = make_test_kek(), make_test_kek()
    async with backend.session() as session:
        store = EncryptedLocalSecretStore()
        await store.bootstrap(session, _raw(old_value))
        await session.commit()
    await backend.dispose()

    config_path = _write_config(tmp_path / "config.yaml", url)
    monkeypatch.setenv(TEST_KEK_ENV_VAR, old_value)
    monkeypatch.setenv("HYPERMIND_NEW_KEK", new_value)

    assert await kek_rotate._run(config_path) == 0
    out = capsys.readouterr()
    assert "re-sealed 1 data key(s)" in out.out
    assert old_value not in out.out + out.err and new_value not in out.out + out.err

    backend = SQLAlchemyStorageBackend(url)
    async with backend.session() as session:
        await EncryptedLocalSecretStore().unlock(session, _raw(new_value))
        with pytest.raises(SecretIntegrityError):
            await EncryptedLocalSecretStore().unlock(session, _raw(old_value))
    await backend.dispose()


async def test_the_cli_refuses_without_a_new_kek(tmp_path, monkeypatch, capsys):
    config_path = _write_config(tmp_path / "config.yaml", database_url_for(tmp_path / "x.db"))
    monkeypatch.setenv(TEST_KEK_ENV_VAR, make_test_kek())
    monkeypatch.delenv("HYPERMIND_NEW_KEK", raising=False)

    assert await kek_rotate._run(config_path) == 1
    assert "refused" in capsys.readouterr().err

