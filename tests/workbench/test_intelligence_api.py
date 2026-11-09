"""docs/30 — the Intelligence Workbench's configuration API.

Through the production composition root: owner isolation, immutable versions
with a canonical hash, optimistic concurrency, catalogue-bound validation,
honest integration status, and the boundary that matters most — saving a
profile changes nothing in production (no model row, no grant, no task path).
"""

from __future__ import annotations

import json
import uuid

import pytest

from server.gateway.app import API_V1_PREFIX
from server.storage.models import (
    AgentConfiguration,
    AuditEvent,
    CapabilityGrant,
    ImmutableRowError,
    IntelligenceProfileRow,
    IntelligenceProfileVersionRow,
)
from shared.schemas.intelligence import ExperimentSnapshot, IntelligenceProfileDocument, profile_hash

V1 = API_V1_PREFIX
CATALOG = f"{V1}/intelligence/catalog"
PROFILES = f"{V1}/intelligence/profiles"
VALIDATE = f"{V1}/intelligence/validate"
PAID = {"input_per_1k_tokens": 0.001, "output_per_1k_tokens": 0.002}


def _config(**workbench) -> dict:
    return {
        "agent": {"provider": "ollama", "model": "qwen-local", "endpoint": "http://127.0.0.1:11434",
                  "fallback": {"provider": "ollama", "model": "qwen-fallback"}},
        "workbench": {
            "enabled": True,
            "models": [{"id": "small-paid", "provider": "openai_compatible", "model": "gpt-small",
                        "endpoint": "https://llm.workbench.test/v1", "pricing": PAID}],
            "orchestration_backends": [
                {"id": "templates.deterministic", "display_name": "Templates", "kind": "workflow_engine",
                 "adapter": "deterministic_templates", "runtime": "jarvis-experiments",
                 "delegation_mode": "fixed_template", "supports_parallel": True, "max_fan_out": 4},
                {"id": "planner.model", "display_name": "Planner", "kind": "planner", "adapter": "model_planner",
                 "runtime": "jarvis-experiments", "requires_model": True, "delegation_mode": "plan_only",
                 "max_fan_out": 3, "max_budget": 0.05},
                {"id": "letta.candidate", "display_name": "Letta (candidate)", "kind": "agentic_runtime",
                 "adapter": "letta", "runtime": "letta-app-server", "requires_model": True,
                 "endpoint_ref": "https://letta.operator.internal:8283", "delegation_mode": "jarvis_mediated",
                 "state_model": "external_persistent", "max_fan_out": 4},
                {"id": "hidden.backend", "display_name": "Hidden", "kind": "workflow_engine",
                 "adapter": "deterministic_templates", "runtime": "x", "delegation_mode": "fixed_template",
                 "enabled": False},
            ],
            **workbench,
        },
    }


def _doc(**overrides) -> dict:
    doc = {
        "central": {"catalog_id": "agent.primary", "prompt_profile": "central.contract", "prompt_version": "1"},
        "orchestration": {"backend_id": "templates.deterministic", "timeout_seconds": 30, "max_fan_out": 3},
        "specialists": [
            {"specialization": "research", "catalog_id": "agent.fallback", "prompt_profile": "research.default",
             "output_schema": "research.v1"},
        ],
        "execution": {"mode": "parallel", "per_call_timeout_seconds": 30, "overall_timeout_seconds": 120},
    }
    doc.update(overrides)
    return doc


async def _create(h, actor, name="baseline", document=None):
    resp = await h.client.post(PROFILES, json={"name": name, "document": document or _doc()}, headers=actor.auth)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_disabled_workbench_authenticates_then_answers_503(make_harness):
    h = await make_harness()
    alice = await h.user("alice")
    assert (await h.client.get(CATALOG)).status_code == 401
    resp = await h.client.get(CATALOG, headers=alice.auth)
    assert resp.status_code == 503
    assert resp.json()["error"]["details"] == {"dependency": "workbench"}


async def test_catalog_is_honest_and_never_shows_an_endpoint_or_key(make_harness):
    h = await make_harness(config=_config())
    alice = await h.user("alice")
    resp = await h.client.get(CATALOG, headers=alice.auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["production_active"] is False
    backends = {b["backend_id"]: b for b in body["orchestration_backends"]}
    assert set(backends) == {"templates.deterministic", "planner.model", "letta.candidate"}   # disabled is hidden
    assert backends["templates.deterministic"]["integration"] == "integrated"
    assert backends["templates.deterministic"]["state"] == "experimental_only"
    # Listed by the operator, but no adapter exists: never shown as connected.
    assert backends["letta.candidate"]["integration"] == "not_integrated"
    assert backends["letta.candidate"]["state"] == "not_integrated"
    assert backends["letta.candidate"]["endpoint_configured"] is True
    assert all(b["production_active"] is False for b in backends.values())
    models = {m["catalog_id"]: m for m in body["models"]}
    assert set(models) == {"agent.primary", "agent.fallback", "workbench.small-paid"}
    assert models["agent.primary"]["production_selectable"] is True
    assert models["workbench.small-paid"]["production_selectable"] is False
    assert models["workbench.small-paid"]["experiment_state"] == "connection_required"
    for needle in ("llm.workbench.test", "letta.operator.internal", "127.0.0.1:11434", "secretstore"):
        assert needle not in resp.text
    assert {s["id"] for s in body["specializations"]} == {"research", "assessment", "planning"}


async def test_create_version_and_read_back_with_hash(make_harness):
    h = await make_harness(config=_config())
    alice = await h.user("alice")
    created = await _create(h, alice)
    assert created["current_version"] == 1
    latest = created["latest"]
    expected = profile_hash(IntelligenceProfileDocument.model_validate(_doc()))
    assert latest["config_hash"] == expected
    assert latest["status"]["production_active"] is False
    assert latest["status"]["runnable_in_harness"] is True
    assert latest["validation"]["valid"] is True

    pid = created["profile_id"]
    changed = _doc(execution={"mode": "sequential", "per_call_timeout_seconds": 30, "overall_timeout_seconds": 60})
    resp = await h.client.post(f"{PROFILES}/{pid}/versions", json={"base_version": 1, "document": changed},
                               headers=alice.auth)
    assert resp.status_code == 201, resp.text
    assert resp.json()["current_version"] == 2
    v1 = (await h.client.get(f"{PROFILES}/{pid}/versions/1", headers=alice.auth)).json()
    v2 = (await h.client.get(f"{PROFILES}/{pid}/versions/2", headers=alice.auth)).json()
    assert v1["config_hash"] == expected and v1["document"]["execution"]["mode"] == "parallel"
    assert v2["config_hash"] != expected and v2["document"]["execution"]["mode"] == "sequential"
    full = (await h.client.get(f"{PROFILES}/{pid}", headers=alice.auth)).json()
    assert [v["version"] for v in full["versions"]] == [2, 1]
    actions = [r.action for r in await h.rows(AuditEvent) if r.action.startswith("config.intelligence_profile")]
    assert actions == ["config.intelligence_profile.created", "config.intelligence_profile.versioned"]


async def test_a_stale_base_version_is_refused_not_overwritten(make_harness):
    h = await make_harness(config=_config())
    alice = await h.user("alice")
    pid = (await _create(h, alice))["profile_id"]
    first = await h.client.post(f"{PROFILES}/{pid}/versions", json={"base_version": 1, "document": _doc()},
                                headers=alice.auth)
    assert first.status_code == 201
    stale = await h.client.post(f"{PROFILES}/{pid}/versions", json={"base_version": 1, "document": _doc()},
                                headers=alice.auth)
    assert stale.status_code == 409
    assert stale.json()["error"]["details"] == {"current_version": 2}


async def test_versions_are_immutable_at_the_storage_layer(make_harness):
    h = await make_harness(config=_config())
    alice = await h.user("alice")
    pid = uuid.UUID((await _create(h, alice))["profile_id"])
    async with h.storage.session() as s:
        row = await s.get(IntelligenceProfileVersionRow, (pid, 1))
        row.document = {**row.document, "schema_version": 1, "central": {"catalog_id": "agent.fallback"}}
        with pytest.raises(ImmutableRowError):
            await s.flush()
        await s.rollback()


async def test_another_users_profile_is_not_found_everywhere(make_harness):
    h = await make_harness(config=_config())
    alice, bob = await h.user("alice"), await h.user("bob")
    pid = (await _create(h, alice))["profile_id"]
    assert (await h.client.get(PROFILES, headers=bob.auth)).json() == {"items": []}
    for method, path, body in [
        ("GET", f"{PROFILES}/{pid}", None),
        ("GET", f"{PROFILES}/{pid}/versions/1", None),
        ("GET", f"{PROFILES}/{pid}/versions/1/snapshot", None),
        ("POST", f"{PROFILES}/{pid}/versions", {"base_version": 1, "document": _doc()}),
        ("DELETE", f"{PROFILES}/{pid}", None),
    ]:
        resp = await h.client.request(method, path, json=body, headers=bob.auth)
        assert resp.status_code == 404, (method, path, resp.text)
    assert (await h.client.get(f"{PROFILES}/{pid}", headers=alice.auth)).status_code == 200


async def test_identity_and_endpoint_fields_are_refused(make_harness):
    h = await make_harness(config=_config())
    alice = await h.user("alice")
    bad = [
        {"name": "x", "document": _doc(), "owner_user_id": str(uuid.uuid4())},
        {"name": "x", "document": {**_doc(), "user_id": str(uuid.uuid4())}},
        {"name": "x", "document": _doc(central={"catalog_id": "agent.primary", "endpoint": "https://evil.test"})},
        {"name": "x", "document": _doc(orchestration={"backend_id": "templates.deterministic",
                                                      "endpoint": "https://evil.test"})},
        {"name": "x", "document": _doc(central={"catalog_id": "agent.primary", "secret_ref": "env:HOME"})},
        {"name": "x", "document": _doc(orchestration={"backend_id": "planner.model", "max_depth": 2,
                                                      "model_catalog_id": "agent.primary"})},
    ]
    for body in bad:
        resp = await h.client.post(PROFILES, json=body, headers=alice.auth)
        assert resp.status_code == 422, (body, resp.text)
        assert "evil.test" not in resp.text
    assert await h.rows(IntelligenceProfileRow) == []


@pytest.mark.parametrize("document, code", [
    (_doc(central={"catalog_id": "workbench.nope"}), "unknown_model"),
    (_doc(orchestration={"backend_id": "hidden.backend"}), "unknown_backend"),
    (_doc(orchestration={"backend_id": "planner.model"}), "model_required"),
    (_doc(orchestration={"backend_id": "templates.deterministic", "model_catalog_id": "agent.primary"}),
     "model_not_used"),
    (_doc(orchestration={"backend_id": "templates.deterministic", "max_fan_out": 9}), "fan_out_exceeds_limit"),
    (_doc(orchestration={"backend_id": "planner.model", "model_catalog_id": "agent.primary"}),
     "parallel_unsupported"),
    (_doc(orchestration={"backend_id": "letta.candidate", "model_catalog_id": "agent.primary"},
          execution={"mode": "sequential"}), "external_state_not_permitted"),
    (_doc(specialists=[{"specialization": "astrology", "catalog_id": "agent.primary",
                        "prompt_profile": "x", "output_schema": "generic.v1"}]), "unknown_specialization"),
    (_doc(specialists=[{"specialization": "research", "catalog_id": "agent.primary",
                        "prompt_profile": "research.default", "output_schema": "planning.v1"}]), "schema_mismatch"),
    (_doc(specialists=[{"specialization": "research", "catalog_id": "agent.primary",
                        "prompt_profile": "research.default", "output_schema": "research.v1",
                        "context_policy": {"mode": "task_only"}}]), "context_too_broad"),
    (_doc(execution={"mode": "parallel", "per_call_budget": 0.04, "overall_budget": 0.9}), "budget_exceeds_limit"),
    (_doc(specialists=[{"specialization": "research", "catalog_id": "agent.primary",
                        "prompt_profile": "research.default", "output_schema": "research.v1", "budget": 0.02}],
          execution={"mode": "parallel", "per_call_budget": 0.02, "overall_budget": 0.01}), "budget_exceeds_limit"),
])
async def test_catalogue_bound_validation_refuses_with_a_stable_code(make_harness, document, code):
    h = await make_harness(config=_config())
    alice = await h.user("alice")
    check = await h.client.post(VALIDATE, json={"document": document}, headers=alice.auth)
    if check.status_code == 422:   # a schema-level refusal (e.g. per_call > overall) comes back the same way
        assert code == "budget_exceeds_limit"
        return
    assert check.status_code == 200, check.text
    assert code in {i["code"] for i in check.json()["issues"]}, check.json()["issues"]
    assert check.json()["valid"] is False
    saved = await h.client.post(PROFILES, json={"name": "x", "document": document}, headers=alice.auth)
    assert saved.status_code == 422
    assert code in {i["code"] for i in saved.json()["error"]["details"]["issues"]}


async def test_snapshot_is_immutable_hash_checked_and_carries_no_endpoint_or_key(make_harness):
    h = await make_harness(config=_config())
    alice = await h.user("alice")
    doc = _doc(orchestration={"backend_id": "templates.deterministic"},
               specialists=[{"specialization": "assessment", "catalog_id": "workbench.small-paid",
                             "prompt_profile": "assessment.default", "output_schema": "assessment.v1",
                             "budget": 0.01}],
               execution={"mode": "sequential", "per_call_budget": 0.01, "overall_budget": 0.1})
    pid = (await _create(h, alice, document=doc))["profile_id"]
    resp = await h.client.get(f"{PROFILES}/{pid}/versions/1/snapshot", headers=alice.auth)
    assert resp.status_code == 200, resp.text
    snap = ExperimentSnapshot.model_validate(resp.json())
    assert snap.production_active is False
    assert snap.profile_version == 1
    assert snap.config_hash == profile_hash(snap.document)
    assert snap.models["workbench.small-paid"].input_per_1k_tokens == PAID["input_per_1k_tokens"]
    assert snap.backend.integration.value == "integrated"
    assert "llm.workbench.test" not in resp.text and "endpoint" not in resp.text and "secret" not in resp.text


async def test_a_version_that_no_longer_fits_the_catalogue_cannot_be_exported(make_harness):
    h = await make_harness(config=_config())
    alice = await h.user("alice")
    pid = (await _create(h, alice))["profile_id"]
    h.config.workbench.orchestration_backends[0].enabled = False   # the operator withdraws it
    view = (await h.client.get(f"{PROFILES}/{pid}", headers=alice.auth)).json()
    assert view["latest"]["validation"]["valid"] is False
    assert view["latest"]["status"]["overall"] == "validation_failed"
    resp = await h.client.get(f"{PROFILES}/{pid}/versions/1/snapshot", headers=alice.auth)
    assert resp.status_code == 409


async def test_a_not_integrated_backend_saves_with_a_warning_but_is_not_runnable(make_harness):
    h = await make_harness(config=_config())
    alice = await h.user("alice")
    h.config.workbench.orchestration_backends[2].state_model = "run_scoped"   # make it otherwise acceptable
    doc = _doc(orchestration={"backend_id": "letta.candidate", "model_catalog_id": "agent.primary"},
               execution={"mode": "sequential"})
    created = await _create(h, alice, document=doc)
    latest = created["latest"]
    assert latest["validation"]["valid"] is True
    assert {"code": "not_integrated", "severity": "warning"}.items() <= next(
        i for i in latest["validation"]["issues"] if i["code"] == "not_integrated").items()
    assert latest["status"]["orchestration"] == "not_integrated"
    assert latest["status"]["runnable_in_harness"] is False


async def test_saving_a_profile_changes_nothing_in_production(make_harness):
    h = await make_harness(config=_config())
    alice = await h.user("alice")
    before = (await h.client.get(f"{V1}/config/model", headers=alice.auth)).json()
    grants_before = len(await h.rows(CapabilityGrant))
    await _create(h, alice, document=_doc(central={"catalog_id": "agent.fallback"}))
    assert (await h.client.get(f"{V1}/config/model", headers=alice.auth)).json() == before
    assert await h.rows(AgentConfiguration) == []
    assert len(await h.rows(CapabilityGrant)) == grants_before


async def test_delete_removes_versions_and_is_audited(make_harness):
    h = await make_harness(config=_config())
    alice = await h.user("alice")
    pid = (await _create(h, alice))["profile_id"]
    assert (await h.client.delete(f"{PROFILES}/{pid}", headers=alice.auth)).status_code == 204
    assert await h.rows(IntelligenceProfileRow) == []
    assert await h.rows(IntelligenceProfileVersionRow) == []
    assert "config.intelligence_profile.deleted" in [r.action for r in await h.rows(AuditEvent)]


async def test_profile_limit_per_user(make_harness):
    h = await make_harness(config=_config(limits={"max_profiles_per_user": 1}))
    alice, bob = await h.user("alice"), await h.user("bob")
    await _create(h, alice)
    resp = await h.client.post(PROFILES, json={"name": "two", "document": _doc()}, headers=alice.auth)
    assert resp.status_code == 409
    await _create(h, bob)   # the limit is per user


async def test_stored_documents_hold_no_identity_endpoint_or_key(make_harness):
    h = await make_harness(config=_config())
    alice = await h.user("alice")
    await _create(h, alice)
    for row in await h.rows(IntelligenceProfileVersionRow):
        blob = json.dumps(row.document)
        for needle in ("user_id", "endpoint", "secret", "key_ref", str(alice.user_id)):
            assert needle not in blob
