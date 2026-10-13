"""`GET /api/v1/ready` (pilot plan F10/C3) and the deployment preconditions
(register §2N readings 5 and 6, confirmed 2026-10-13).

`/ready` is public and says only booleans: 200 when the store answers, its
schema is at the code's Alembic head and the SecretStore is unlocked; 503
otherwise. `/health` stays the always-200 liveness probe.
"""

from __future__ import annotations

import httpx
import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import text

from server.config.errors import ConfigError
from server.config.preconditions import check_deployment, deployment_problems
from server.gateway.app import API_V1_PREFIX, create_app
from server.gateway.routers.health import _MIGRATIONS
from server.gateway.security import build_security_core
from server.secrets.kek import resolve_kek
from tests.support import TEST_CLIENT_ID, TEST_ISSUER, TEST_KEK_ENV_VAR, make_test_config, make_test_kek

HEADS = ScriptDirectory(str(_MIGRATIONS)).get_heads()


async def _stamp(storage, revisions) -> None:
    async with storage.session() as session:
        await session.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"))
        await session.execute(text("DELETE FROM alembic_version"))
        for revision in revisions:
            await session.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": revision})
        await session.commit()


@pytest.fixture(autouse=True)
def _kek(monkeypatch):
    # One KEK per test: bootstrapping twice in a test is an unlock under it.
    monkeypatch.setenv(TEST_KEK_ENV_VAR, make_test_kek())


async def _get_ready(storage, *, unlocked: bool, monkeypatch) -> httpx.Response:
    core = build_security_core(make_test_config())
    if unlocked:
        async with storage.session() as session:
            await core.secret_store.bootstrap(session, resolve_kek(f"env:{TEST_KEK_ENV_VAR}"))
            await session.commit()
    app = create_app(storage=storage, security=core)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(f"{API_V1_PREFIX}/ready")


async def test_ready_when_migrated_and_unlocked(storage, monkeypatch):
    await _stamp(storage, HEADS)
    resp = await _get_ready(storage, unlocked=True, monkeypatch=monkeypatch)
    assert resp.status_code == 200
    assert resp.json() == {"ready": True, "database": True, "migrations": True, "secrets": True}
    assert resp.headers["cache-control"] == "no-store"


async def test_not_ready_while_the_secret_store_is_locked(storage, monkeypatch):
    await _stamp(storage, HEADS)
    resp = await _get_ready(storage, unlocked=False, monkeypatch=monkeypatch)
    assert resp.status_code == 503
    assert resp.json() == {"ready": False, "database": True, "migrations": True, "secrets": False}


async def test_not_ready_without_migrations_or_behind_head(storage, monkeypatch):
    # Never migrated (no alembic_version table at all).
    resp = await _get_ready(storage, unlocked=True, monkeypatch=monkeypatch)
    assert (resp.status_code, resp.json()["migrations"]) == (503, False)
    # Migrated, but to an older revision.
    await _stamp(storage, ["0000000000"])
    resp = await _get_ready(storage, unlocked=True, monkeypatch=monkeypatch)
    assert (resp.status_code, resp.json()["migrations"]) == (503, False)


async def test_ready_says_booleans_only(storage, monkeypatch):
    await _stamp(storage, HEADS)
    body = (await _get_ready(storage, unlocked=True, monkeypatch=monkeypatch)).json()
    assert set(body) == {"ready", "database", "migrations", "secrets"}
    assert all(isinstance(value, bool) for value in body.values())


async def test_health_is_unchanged_and_always_200(storage, monkeypatch):
    app = create_app(storage=storage, security=build_security_core(make_test_config()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"{API_V1_PREFIX}/health")
    assert resp.status_code == 200


# ── deployment preconditions (readings 5 and 6) ────────────────────────────

_REMOTE = {
    "provider": "openai_compatible",
    "model": "example-model",
    "endpoint": "https://models.example.test/v1",
    "secret_ref": "env:PILOT_MODEL_KEY",
    "pricing": {"input_per_1k_tokens": 0.001, "output_per_1k_tokens": 0.002},
}


def _config(*, budgets: tuple[float, float, float], allowed: list[str]):
    task, user, total = budgets
    return make_test_config(
        agent={**_REMOTE, "bounds": {"per_task_budget": task}},
        security={
            "oidc": {"client_id": TEST_CLIENT_ID, "issuer": TEST_ISSUER, "allowed_emails": allowed},
            "budgets": {"per_user_daily_cost_limit": user, "global_daily_cost_limit": total},
        },
    )


def test_a_local_primary_has_no_preconditions():
    assert deployment_problems(make_test_config()) == []


def test_a_remote_primary_with_budgets_and_an_allow_list_starts():
    config = _config(budgets=(0.05, 1.0, 10.0), allowed=["alice@example.test"])
    assert check_deployment(config) is config


@pytest.mark.parametrize("budgets", [(0.0, 1.0, 10.0), (0.05, 0.0, 10.0), (0.05, 1.0, 0.0)])
def test_a_remote_primary_without_every_budget_does_not_start(budgets):
    with pytest.raises(ConfigError, match="OD-PILOT-2"):
        check_deployment(_config(budgets=budgets, allowed=["alice@example.test"]))


def test_a_remote_primary_without_an_allow_list_does_not_start():
    with pytest.raises(ConfigError, match="OD-PILOT-5"):
        check_deployment(_config(budgets=(0.05, 1.0, 10.0), allowed=[]))


def test_the_entrypoint_checks_the_preconditions_before_building_the_app():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "server" / "composition" / "main.py").read_text()
    assert "check_deployment(load_config())" in source
