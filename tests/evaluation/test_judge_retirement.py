"""D-JDG-RETIRE: the LLM Judge and its global improvement path are gone, not dormant.

Each test proves one way the retired subsystem could come back — or keep
acting — does not exist:

| Retired | Proved absent by |
|---|---|
| the LLM evaluator, its metering, its candidate registry | `test_the_judge_modules_are_gone` |
| a configuration that turns it (or its stop requests / budget) back on | `test_every_retired_judge_key_is_refused_by_name`, `test_only_the_rules_evaluator_can_be_configured` |
| its routes (switches, approve, reject, rollback, agent purpose suggestions) | `test_no_judge_route_is_mounted`, `test_retired_routes_answer_not_found` |
| any writer of the inert tables | `test_no_server_code_writes_the_inert_judge_tables` |
| the global prompt / tool-description / recovery tuning | `test_historical_approved_versions_never_reach_a_worker_prompt` |
| a background worker when evaluation is off | `test_no_evaluation_worker_exists_when_disabled` |
| Judge-specific secret resolution | `test_evaluation_wiring_resolves_no_secret_and_calls_no_model` |
"""

from __future__ import annotations

import ast
import importlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from server.config.schema import RETIRED_EVALUATION_KEYS, EvaluationConfig
from server.storage.models import ConfigVersion, EvaluationControl, ImprovementCandidateRow
from tests.evaluation.conftest import SU
from tests.runtime.conftest import ask, call, final

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("module", [
    "server.evaluation.provider", "server.evaluation.metering", "server.evaluation.candidates",
    "server.composition.improvements", "server.gateway.evaluation_control_port",
    "server.gateway.routers.evaluation_control", "server.agents.revision",
])
def test_the_judge_modules_are_gone(module):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


@pytest.mark.parametrize("key, value", [
    ("provider", {"provider": "ollama", "model": "judge"}),
    ("live", {"enabled": True}),
    ("may_request_stop", True),
    ("stop_alert_threshold", 5),
    ("stop_alert_window_minutes", 60),
    ("budget", 1.0),
    ("budget_scope", "own"),
])
def test_every_retired_judge_key_is_refused_by_name(key, value):
    assert key in RETIRED_EVALUATION_KEYS
    with pytest.raises(ValidationError, match="retired"):
        EvaluationConfig.model_validate({"enabled": True, "evaluator": "rules", key: value})
    # Even with evaluation off: a config that names the Judge is wrong about the system.
    with pytest.raises(ValidationError, match="retired"):
        EvaluationConfig.model_validate({"enabled": False, key: value})


def test_only_the_rules_evaluator_can_be_configured():
    with pytest.raises(ValidationError, match="retired"):
        EvaluationConfig.model_validate({"enabled": True, "evaluator": "llm"})
    with pytest.raises(ValidationError):
        EvaluationConfig.model_validate({"enabled": True, "evaluator": "anything_else"})
    assert EvaluationConfig.model_validate({"enabled": True}).evaluator == "rules"
    assert EvaluationConfig().enabled is False


RETIRED_PATHS = ("/admin/control/evaluation", "/candidates")


@pytest.mark.asyncio
async def test_no_judge_route_is_mounted(evaluated):
    h = await evaluated()
    paths = {getattr(r, "path", "") for r in h.app.routes}
    assert not [p for p in paths if any(fragment in p for fragment in RETIRED_PATHS)]
    assert not [p for p in paths if "rubric" in p or "config-versions" in p or "improvement" in p]


@pytest.mark.asyncio
@pytest.mark.parametrize("method, path", [
    ("POST", "/api/v1/admin/control/evaluation/switches"),
    ("POST", f"/api/v1/admin/control/evaluation/candidates/{uuid.uuid4()}/approve"),
    ("POST", f"/api/v1/admin/control/evaluation/candidates/{uuid.uuid4()}/reject"),
    ("POST", "/api/v1/admin/control/evaluation/config-versions/1/rollback"),
])
async def test_retired_routes_answer_not_found(evaluated, method, path):
    h = await evaluated()
    resp = await h.client.request(method, path, json={"reason": "x", "judge_enabled": True}, headers=SU)
    assert resp.status_code in (404, 405), resp.text


# Writing ORM constructs: `Row(...)` (insert), attribute assignment on a fetched
# row is caught by forbidding any reference outside the allow-list below.
_INERT = {ImprovementCandidateRow.__name__, ConfigVersion.__name__, EvaluationControl.__name__}
# Where a reference is still legitimate, and why:
_ALLOWED = {
    "server/storage/models.py": "the ORM class itself (kept so the schema and models agree)",
    "server/agents/service.py": "deletes an agent's historical suggestions with the agent",
    "server/dashboard/console.py": "counts the retired rows (read-only, no content)",
}


def test_no_server_code_writes_the_inert_judge_tables():
    offenders: dict[str, set[str]] = {}
    for path in (REPO / "server").rglob("*.py"):
        rel = path.relative_to(REPO).as_posix()
        if "/migrations/" in rel:
            continue  # history: migrations create the tables
        names = {n.id for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.Attribute)}
        hit = names & _INERT
        if hit and rel not in _ALLOWED:
            offenders[rel] = hit
    assert offenders == {}
    # The allowed references are only a delete and counts — never a constructor call.
    for rel in _ALLOWED:
        if rel == "server/storage/models.py":
            continue
        tree = ast.parse((REPO / rel).read_text())
        constructed = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                       and isinstance(n.func, ast.Name) and n.func.id in _INERT]
        assert constructed == [], rel
    service = (REPO / "server/agents/service.py").read_text()
    assert "delete(ImprovementCandidateRow)" in service
    assert service.count("ImprovementCandidateRow") == 4  # import, delete, its two where-clauses


@pytest.mark.asyncio
async def test_historical_approved_versions_never_reach_a_worker_prompt(evaluated):
    """The pre-retirement data is still in the database — and inert. A
    `worker.system_prompt`, a tool description and recovery bounds approved
    before the retirement change nothing for any user."""

    h = await evaluated()
    recovery_before = h.runtime._recovery
    planted = "HISTORICAL-APPROVED-GUIDANCE-7f3a"
    async with h.storage.session() as s:
        now = datetime.now(timezone.utc)
        for key, value in (("worker.system_prompt", planted),
                           ("worker.tool_description:files.read", planted + "-tool"),
                           ("recovery.loop_repeat_limit", "2")):
            s.add(ConfigVersion(target_key=key, value=value, action="approve", reason="historical",
                                created_at=now, created_by="pre-retirement"))
        await s.commit()
    bob = await h.user("bob")
    await h.grant(bob, "file.read")
    h.model.push(ask("file.read"), call("files.read", "list_directory"), final("hello bob"))
    assert (await h.submit(bob, "what's in my files")).status_code == 200
    prompts = [messages[0].content for messages in h.model.seen]
    assert prompts and all(planted not in p for p in prompts)
    assert all("OPERATOR GUIDANCE" not in p for p in prompts)
    assert h.runtime._recovery is recovery_before  # recovery bounds come from the config file only


@pytest.mark.asyncio
async def test_no_evaluation_worker_exists_when_disabled(make_harness):
    h = await make_harness()
    assert h.app.state.evaluation is None
    assert h.runtime._observer is None
    background = getattr(h.app.state, "background", None) or []
    assert not [b for b in background if type(b).__name__ == "EvaluationQueue"]


def test_evaluation_wiring_resolves_no_secret_and_calls_no_model():
    for rel in ("server/composition/evaluation.py", *[p.relative_to(REPO).as_posix()
                                                       for p in (REPO / "server/evaluation").glob("*.py")]):
        tree = ast.parse((REPO / rel).read_text())
        imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not [m for m in imported if m.startswith(("server.secrets", "server.models", "server.modeltools",
                                                         "server.composition.secret_context",
                                                         "server.composition.models", "httpx"))], rel
