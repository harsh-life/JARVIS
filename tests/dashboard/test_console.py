"""The operator console through the real application (28, DASH-001..006).

| Hook | Tests |
|---|---|
| DSH-T1 every console route is GET | `test_every_console_route_is_get_and_superuser_only` (+ `test_console_readonly.py`) |
| DSH-T2 no user token reaches /admin or /admin/control | `test_no_ordinary_or_forged_credential_reaches_any_admin_route` |
| DSH-T3 no secret value; config = handles + resolvability | `test_no_view_ever_contains_a_secret_value`, `test_configuration_shows_handles_and_whether_they_resolve` |
| DSH-T4 redacted by default; unredacted view audited | `test_user_content_is_redacted_by_default`, `test_the_unredacted_view_is_privileged_and_audited` |
| DSH-T5 persistent banners | `test_the_global_stop_banner_persists_until_cleared`, `test_the_break_glass_banner` |
"""

from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio
from fastapi.routing import APIRoute

from server.gateway.routers import admin
from server.secrets.requester import SecretRequester
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.security.superuser import SUPERUSER_TOKEN_ENV
from server.storage.models import AuditEvent
from shared.schemas.enums import SecretClass, SecretOwnerScopeType
from tests.evaluation.conftest import drain, verdict
from tests.runtime.conftest import ScriptedModel, ask, call, final

pytestmark = pytest.mark.asyncio

TOKEN = "TEST-ONLY-superuser-credential-0123456789abcdef"
SU = {"Authorization": f"Superuser {TOKEN}"}
ADMIN = "/api/v1/admin"
VIEWS = ["banner", "health", "tasks", "recovery", "break-glass", "evaluations", "usage", "memory", "devices",
         "audit", "configuration"]
SECRET = "sk-proj-PLANTEDsecretVALUE0123456789abcd"  # TEST-ONLY fixture
PRIVATE_TEXT = "my private medical appointment is on tuesday"


@pytest_asyncio.fixture
async def console(make_harness, monkeypatch):
    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("JUDGE_KEY_FOR_TEST", SECRET)
    judge = ScriptedModel("scripted-judge")
    h = await make_harness(config={
        "evaluation": {
            "enabled": True,
            "provider": {"provider": "openai", "model": "scripted-judge", "endpoint": "https://judge.invalid/v1",
                         "secret_ref": "env:JUDGE_KEY_FOR_TEST",
                         "pricing": {"input_per_1k_tokens": 0.0, "output_per_1k_tokens": 0.0}},
            "post_hoc": {"sample_successful": 1.0},
        },
        "agent": {"provider": "ollama", "model": "scripted-primary",
                  "fallback": {"provider": "openai", "model": "fallback-model", "endpoint": "https://x.invalid/v1",
                               "secret_ref": "env:UNSET_FALLBACK_KEY",
                               "pricing": {"input_per_1k_tokens": 0.0, "output_per_1k_tokens": 0.0}}},
    }, models={"scripted-judge": judge})
    h.judge = judge
    return h


async def populate(h) -> dict:
    """A task with private content, a secret in its observation, an evaluation
    with a note and a candidate — everything the console must not leak."""

    alice = await h.user("alice")
    await h.grant(alice, "file.read")

    async def leaky(invocation):
        from shared.schemas.agent import ToolOutput

        return ToolOutput(ok=True, content=f"token: {SECRET}")

    h.reads.execute = leaky
    h.judge.push(verdict(
        quality=0.4, failures=[{"step_ref": "s1", "category": "slow_start", "note": PRIVATE_TEXT}],
        improvement_candidates=[{"target": "worker.system_prompt", "proposed_change": PRIVATE_TEXT}],
    ))
    h.model.push(ask("file.read"), call("files.read", "list_directory"), final(PRIVATE_TEXT))
    resp = await h.submit(alice, PRIVATE_TEXT)
    assert resp.status_code == 200, resp.text
    await drain(h)
    async with h.storage.session() as s:
        handle = await h.core.secret_store.set(
            s, owner_scope_type=SecretOwnerScopeType.SERVER, owner_scope_id=None,
            secret_class=SecretClass.MODEL_API_KEY, value=SECRET, requester=SecretRequester.server(),
            audit=AuditLogger(s, request_id=uuid.uuid4()),
        )
        await s.commit()
    return {"alice": alice, "task_id": resp.json()["task_id"], "handle": handle}


async def all_views(h, headers=SU) -> dict[str, dict]:
    out = {}
    for view in VIEWS:
        resp = await h.client.get(f"{ADMIN}/{view}", headers=headers)
        assert resp.status_code == 200, (view, resp.text)
        out[view] = resp.json()
    return out


# ── DSH-T1 / DSH-T2 ────────────────────────────────────────────────────────


def _admin_paths(app) -> dict[str, set[str]]:
    """Every registered `/admin` path and its methods, from the app's own schema."""

    return {path: {m.upper() for m in ops} for path, ops in app.openapi()["paths"].items()
            if path.startswith(ADMIN)}


async def test_every_console_route_is_get_and_superuser_only(console):
    views = {p: m for p, m in _admin_paths(console.app).items() if not p.startswith(f"{ADMIN}/control")}
    assert {p.removeprefix(f"{ADMIN}/") for p in views} >= set(VIEWS)
    for path, methods in views.items():
        assert methods == {"GET"}, path
    assert all(isinstance(r, APIRoute) and r.methods == {"GET"} for r in admin.router.routes)
    # And the router refuses to be built otherwise.
    probe = admin.APIRouter()

    @probe.post("/x")
    async def _mutate() -> None: ...

    with pytest.raises(RuntimeError):
        admin._assert_read_only(probe)


async def test_no_ordinary_or_forged_credential_reaches_any_admin_route(console):
    alice = await console.user("alice")
    forged = [
        {},
        alice.auth,
        {**alice.auth, "X-Role": "superuser", "X-Admin": "true", "X-Forwarded-User": "operator"},
        {"Authorization": f"Superuser {alice.token}"},
        {"Authorization": f"Bearer {TOKEN}"},
        {"Authorization": f"superuser {TOKEN}"},
        {"Authorization": f"Superuser  {TOKEN}"},
        {"Authorization": f"Superuser {TOKEN}x"},
        {"Cookie": f"superuser={TOKEN}"},
    ]
    targets = [("GET", f"{ADMIN}/{v}") for v in VIEWS] + [
        ("GET", f"{ADMIN}/privileged/tasks/{uuid.uuid4()}?reason=look"),
        ("POST", f"{ADMIN}/control/stop"), ("POST", f"{ADMIN}/control/global-stop"),
        ("POST", f"{ADMIN}/control/global-clear"), ("POST", f"{ADMIN}/control/break-glass"),
        ("GET", f"{ADMIN}/control/break-glass"),
        ("POST", f"{ADMIN}/control/evaluation/switches"),
        ("POST", f"{ADMIN}/control/evaluation/candidates/{uuid.uuid4()}/approve"),
        ("POST", f"{ADMIN}/control/evaluation/config-versions/1/rollback"),
    ]
    for headers in forged:
        for method, url in targets:
            resp = await console.client.request(method, url, headers=headers, json={"reason": "x"})
            assert resp.status_code == 401, (headers, url, resp.text)
            assert "data" not in resp.json()


async def test_every_view_answers_with_its_data_and_the_banners(console):
    await populate(console)
    views = await all_views(console)
    for view in VIEWS[1:]:
        assert set(views[view]) == {"view", "banners", "data"}
    health = views["health"]["data"]["components"]
    assert health["database"]["status"] == "ok"
    assert health["secret_store"]["status"] == "unlocked"
    assert {e["role"] for e in health["model_providers"]["entries"]} >= {"agent.primary", "agent.fallback",
                                                                        "evaluation.provider"}
    assert health["judge"]["judge_enabled"] is True and health["judge"]["queue"]["completed"] == 1
    tasks = views["tasks"]["data"]
    assert tasks["counts"]["terminal"] == 1 and tasks["tasks"][0]["status"] == "completed"
    evaluations = views["evaluations"]["data"]
    assert evaluations["evaluations"][0]["quality"] == 0.4
    assert evaluations["candidates"][0]["status"] == "pending"
    usage = views["usage"]["data"]
    assert usage["global"]["last_24h"]["calls"] >= 3 and usage["global"]["judge_last_24h"]["calls"] == 1
    devices = views["devices"]["data"]["devices"]
    assert len(devices) == 1 and devices[0]["revoked"] is False
    audit = views["audit"]["data"]["events"]
    assert any(e["action"] == "evaluation.recorded" for e in audit)
    filtered = (await console.client.get(f"{ADMIN}/audit?action=evaluation.*&result=success", headers=SU)).json()
    assert filtered["data"]["events"] and all(e["action"].startswith("evaluation.") for e in filtered["data"]["events"])


# ── DSH-T3 ─────────────────────────────────────────────────────────────────


async def test_no_view_ever_contains_a_secret_value(console):
    await populate(console)
    views = await all_views(console)
    probed = (await console.client.get(f"{ADMIN}/health?probe_models=true", headers=SU)).json()
    blob = json.dumps(views) + json.dumps(probed)
    assert SECRET not in blob
    assert TOKEN not in blob
    # The planted secret exists in the SecretStore and in the environment —
    # the console knows *that*, never *what*.
    assert "PLANTEDsecret" not in blob


async def test_configuration_shows_handles_and_whether_they_resolve(console):
    await populate(console)
    config = (await console.client.get(f"{ADMIN}/configuration", headers=SU)).json()["data"]["effective"]
    assert config["evaluation"]["provider"]["secret_ref"] == {"handle": "env:JUDGE_KEY_FOR_TEST", "resolves": True}
    assert config["agent"]["fallback"]["secret_ref"] == {"handle": "env:UNSET_FALLBACK_KEY", "resolves": False}
    assert config["secrets"]["kek_source"]["handle"].startswith("env:")
    assert config["secrets"]["kek_source"]["resolves"] is True


async def test_a_credential_inside_an_ordinary_setting_is_masked(make_harness, monkeypatch):
    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, TOKEN)
    h = await make_harness()
    h.config.__dict__["database_url"] = "postgresql+asyncpg://jarvis:hunter2-db-password@db.internal/jarvis"  # TEST-ONLY fixture
    blob = json.dumps((await h.client.get(f"{ADMIN}/configuration", headers=SU)).json())
    assert "hunter2-db-password" not in blob


# ── DSH-T4 ─────────────────────────────────────────────────────────────────


async def test_user_content_is_redacted_by_default(console):
    seeded = await populate(console)
    blob = json.dumps(await all_views(console))
    assert PRIVATE_TEXT not in blob
    task = (await console.client.get(f"{ADMIN}/tasks", headers=SU)).json()["data"]["tasks"][0]
    assert task["response"] == {"redacted": True, "chars": len(PRIVATE_TEXT)}
    candidate = (await console.client.get(f"{ADMIN}/evaluations", headers=SU)).json()["data"]["candidates"][0]
    assert candidate["proposed_value"]["redacted"] is True
    assert seeded["task_id"] in blob  # ids and results are visible; content is not


async def test_the_unredacted_view_is_privileged_and_audited(console):
    seeded = await populate(console)
    url = f"{ADMIN}/privileged/tasks/{seeded['task_id']}"

    assert (await console.client.get(url, headers=SU)).status_code == 422  # a reason is required
    assert (await console.client.get(url + "?reason=Because I want to", headers=SU)).status_code == 422
    assert (await console.client.get(url + "?reason=support_case", headers=seeded["alice"].auth)).status_code == 401

    resp = await console.client.get(url + "?reason=support_case", headers=SU)
    assert resp.status_code == 200, resp.text
    content = resp.json()["data"]
    assert content["response"] == PRIVATE_TEXT
    assert content["evaluations"][0]["failures"][0]["note"] == PRIVATE_TEXT
    assert content["candidates"][0]["proposed_value"] == PRIVATE_TEXT
    assert SECRET not in json.dumps(content)  # even unredacted, never a secret
    views = [r for r in await console.rows(AuditEvent) if r.action == AuditAction.CONSOLE_UNREDACTED_VIEW.value]
    assert [v.resource for v in views] == [f"console:unredacted:task:{seeded['task_id']}:support_case"]
    assert views[0].actor.value == "superuser"

    missing = await console.client.get(f"{ADMIN}/privileged/tasks/{uuid.uuid4()}?reason=probe", headers=SU)
    assert missing.status_code == 404
    audited = [r for r in await console.rows(AuditEvent) if r.action == AuditAction.CONSOLE_UNREDACTED_VIEW.value]
    assert len(audited) == 2  # the attempt is recorded even when nothing was found


# ── DSH-T5 ─────────────────────────────────────────────────────────────────


async def test_the_global_stop_banner_persists_until_cleared(console):
    assert (await console.client.get(f"{ADMIN}/banner", headers=SU)).json()["banners"] == []
    latched = await console.client.post(f"{ADMIN}/control/global-stop", json={"reason": "incident"}, headers=SU)
    assert latched.status_code == 200
    for view in VIEWS[1:]:
        banners = (await console.client.get(f"{ADMIN}/{view}", headers=SU)).json()["banners"]
        assert [b["kind"] for b in banners] == ["global_stop_latched"], view
        assert banners[0]["reason"] == "incident"
    recovery = (await console.client.get(f"{ADMIN}/recovery", headers=SU)).json()["data"]
    assert recovery["global_latch"]["latched"] is True
    await console.client.post(f"{ADMIN}/control/global-clear", json={}, headers=SU)
    assert (await console.client.get(f"{ADMIN}/banner", headers=SU)).json()["banners"] == []


async def test_the_break_glass_banner(make_harness, monkeypatch, tmp_path):
    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, TOKEN)
    h = await make_harness(config={"execution": {
        "filesystem": {"base_root": str(tmp_path / "sandboxes")},
        "process": {"break_glass": {"enabled": True, "allowed_executables": ["cat"]}},
    }})
    [banner] = (await h.client.get(f"{ADMIN}/banner", headers=SU)).json()["banners"]
    assert banner["kind"] == "break_glass_enabled" and banner["active_records"] == 0
    data = (await h.client.get(f"{ADMIN}/break-glass", headers=SU)).json()
    assert data["data"]["enabled"] is True and data["banners"] == [banner]


# ── the dashboard shows; controls live elsewhere ───────────────────────────


async def test_actions_go_to_the_owning_subsystem_not_the_dashboard(console):
    """A stop issued from "the console" is a `/admin/control/*` call, handled by
    the supervisor's module; the dashboard's module has no route that accepts
    it, and its views only ever report the result."""

    before = len(await console.rows(AuditEvent))
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        for view in VIEWS:
            resp = await console.client.request(method, f"{ADMIN}/{view}", headers=SU, json={})
            # Refused by routing before any handler (the gateway's error
            # handler reports starlette's 405 in its own envelope).
            assert resp.status_code >= 400 and "Method Not Allowed" in resp.text, (method, view, resp.text)
    assert len(await console.rows(AuditEvent)) == before  # nothing ran, not even authentication
    from server.gateway.routers import control, evaluation_control

    control_paths = {p for p in _admin_paths(console.app) if p.startswith(f"{ADMIN}/control")}
    owned = {f"/api/v1{r.path}" for r in (*control.router.routes, *evaluation_control.router.routes)}
    assert control_paths and control_paths == owned
    assert not ({f"/api/v1{r.path}" for r in admin.router.routes} & control_paths)
