"""Stage 5's module boundaries (19 §3 JDG-T2, 28 §4 DSH-T1, 16 §6).

Each contract is declared in `pyproject.toml`, kept by the real repository,
and — the part that makes it a control rather than a comment — catches a
deliberate violation injected into a minimal fixture project.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

JUDGE = "The Judge is never an authority (19, JDG-T2)"
RUNTIME = "The runtime never depends on the Judge (19, JDG-T2)"
JUDGE_IO = "The Judge opens no process or socket (19, REPO-T5)"
DASHBOARD = "The dashboard is read-only: no mutation or control path (28, DASH-002, DSH-T1)"
DASHBOARD_SECRETS = "Dashboard cannot import secrets (16 §5, DASH-002, REPO-T6)"
SUPERUSER = "Only the gateway reaches superuser authority (12 §4, SUPER-001)"
LAYERS = "Track B server layering (16 §2)"
CONTRACTS = (JUDGE, RUNTIME, JUDGE_IO, DASHBOARD, DASHBOARD_SECRETS, SUPERUSER, LAYERS)


def _lint(config: Path, cwd: Path) -> subprocess.CompletedProcess:
    script = Path(sys.executable).parent / "lint-imports"
    return subprocess.run([str(script), "--config", str(config), "--no-cache"], cwd=cwd,
                          capture_output=True, text=True)


def test_every_stage5_contract_is_declared_and_kept():
    result = _lint(REPO / "pyproject.toml", REPO)
    assert result.returncode == 0, result.stdout
    flat = re.sub(r"\s+", " ", result.stdout)
    for name in CONTRACTS:
        assert re.search(re.escape(name) + r" ?KEPT", flat), name
    contracts = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["importlinter"]["contracts"]
    superuser = next(c for c in contracts if c["name"] == SUPERUSER)
    for module in ("server.gateway.evaluation_control_port", "server.gateway.routers.evaluation_control",
                   "server.gateway.console_port", "server.gateway.routers.admin",
                   "server.composition.improvements", "server.composition.evaluation",
                   "server.composition.console"):
        assert module in superuser["forbidden_modules"], module
    for source in ("server.evaluation", "server.dashboard"):
        assert source in superuser["source_modules"]


_PACKAGES = [
    "server", "server/agent", "server/tools", "server/modeltools", "server/models", "server/execution",
    "server/fs", "server/net", "server/capabilities", "server/graph", "server/auth", "server/secrets",
    "server/memory", "server/vault", "server/evaluation", "server/intelligence", "server/scheduler",
    "server/voice", "server/dashboard", "server/gateway", "server/gateway/routers", "server/composition",
    "server/security", "server/storage", "server/config", "shared", "shared/schemas",
]
_MODULES = [
    "server/security/audit.py", "server/security/usage.py", "server/security/superuser.py",
    "server/security/secret_patterns.py", "server/storage/models.py", "server/storage/idempotency.py",
    "server/gateway/superuser_auth.py", "server/gateway/control_port.py", "server/gateway/console_port.py",
    "server/gateway/evaluation_control_port.py", "server/gateway/routers/control.py",
    "server/gateway/routers/admin.py", "server/gateway/routers/evaluation_control.py",
    "server/composition/supervisor.py", "server/composition/latch.py", "server/composition/break_glass.py",
    "server/composition/improvements.py", "server/composition/evaluation.py", "server/composition/console.py",
    "server/capabilities/grants.py", "server/capabilities/confirmation.py", "server/secrets/store.py",
    "server/secrets/audit_port.py", "server/models/provider.py", "server/config/schema.py",
    "shared/schemas/evaluation.py",
]


def _fixture(tmp_path: Path, violation: tuple[str, str]) -> Path:
    root = tmp_path / "fixture"
    for pkg in _PACKAGES:
        (root / pkg).mkdir(parents=True, exist_ok=True)
        (root / pkg / "__init__.py").write_text("")
    for module in _MODULES:
        (root / module).write_text("")
    (root / "server/security/audit.py").write_text("import server.secrets.audit_port\n")
    # What the real packages legitimately import, so a clean fixture is clean.
    (root / "server/evaluation/ok.py").write_text(
        "import server.models.provider\nimport server.security.secret_patterns\n"
        "import server.config.schema\nimport shared.schemas.evaluation\n"
    )
    (root / "server/dashboard/ok.py").write_text(
        "import server.storage.models\nimport server.config.schema\nimport shared.schemas.evaluation\n"
        "import server.security.secret_patterns\n"
    )
    source, target = violation
    path = root / source
    path.write_text(path.read_text() + f"import {target}\n" if path.exists() else f"import {target}\n")

    contracts = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["importlinter"]["contracts"]
    lines = ["[tool.importlinter]", 'root_packages = ["server", "shared"]', "include_external_packages = true", ""]
    for contract in (c for c in contracts if c["name"] in CONTRACTS):
        lines.append("[[tool.importlinter.contracts]]")
        for key, value in contract.items():
            lines.append(f"{key} = {value!r}".replace("'", '"').replace("True", "true").replace("False", "false"))
        lines.append("")
    (root / "fixture.toml").write_text("\n".join(lines))
    return root


def test_the_clean_fixture_passes(tmp_path):
    root = _fixture(tmp_path, ("server/evaluation/fine.py", "server.models.provider"))
    result = _lint(root / "fixture.toml", root)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("violation, contract", [
    # JDG-T2: the Judge reaches no authority, execution, runtime or operator surface.
    (("server/evaluation/leak.py", "server.capabilities.grants"), JUDGE),
    (("server/evaluation/leak.py", "server.graph"), JUDGE),
    (("server/evaluation/leak.py", "server.secrets.store"), JUDGE),
    (("server/evaluation/leak.py", "server.security.audit"), JUDGE),
    (("server/evaluation/leak.py", "server.execution"), JUDGE),
    (("server/evaluation/leak.py", "server.fs"), JUDGE),
    (("server/evaluation/leak.py", "server.net"), JUDGE),
    (("server/evaluation/leak.py", "server.tools"), JUDGE),
    (("server/evaluation/leak.py", "server.agent"), JUDGE),
    (("server/evaluation/leak.py", "server.auth"), JUDGE),
    (("server/evaluation/leak.py", "server.gateway"), JUDGE),
    (("server/evaluation/leak.py", "server.composition.improvements"), SUPERUSER),
    (("server/evaluation/leak.py", "server.composition.break_glass"), SUPERUSER),
    (("server/evaluation/leak.py", "server.gateway.routers.evaluation_control"), SUPERUSER),
    (("server/evaluation/leak.py", "socket"), JUDGE_IO),
    (("server/evaluation/leak.py", "subprocess"), JUDGE_IO),
    # The runtime never depends on the Judge.
    (("server/agent/leak.py", "server.evaluation"), RUNTIME),
    (("server/tools/leak.py", "server.evaluation"), RUNTIME),
    # DSH-T1: the dashboard shows; it reaches no mutation, control or secret path.
    (("server/dashboard/leak.py", "server.agent"), DASHBOARD),
    (("server/dashboard/leak.py", "server.capabilities.grants"), DASHBOARD),
    (("server/dashboard/leak.py", "server.capabilities.confirmation"), DASHBOARD),
    (("server/dashboard/leak.py", "server.security.audit"), DASHBOARD),
    (("server/dashboard/leak.py", "server.security.usage"), DASHBOARD),
    (("server/dashboard/leak.py", "server.storage.idempotency"), DASHBOARD),
    (("server/dashboard/leak.py", "server.evaluation"), DASHBOARD),
    (("server/dashboard/leak.py", "server.gateway.routers.control"), DASHBOARD),
    (("server/dashboard/leak.py", "server.composition.supervisor"), DASHBOARD),
    (("server/dashboard/leak.py", "server.composition.improvements"), DASHBOARD),
    (("server/dashboard/leak.py", "server.secrets.store"), DASHBOARD_SECRETS),
    (("server/dashboard/leak.py", "server.gateway.superuser_auth"), SUPERUSER),
    (("server/dashboard/leak.py", "subprocess"), DASHBOARD),
    # No lower layer reaches the console or the Judge's control path.
    (("server/tools/leak.py", "server.gateway.routers.admin"), SUPERUSER),
    (("server/memory/leak.py", "server.gateway.console_port"), SUPERUSER),
    (("server/agent/leak.py", "server.composition.evaluation"), SUPERUSER),
])
def test_each_boundary_catches_a_deliberate_violation(tmp_path, violation, contract):
    root = _fixture(tmp_path, violation)
    result = _lint(root / "fixture.toml", root)
    assert result.returncode != 0, result.stdout
    flat = re.sub(r"\s+", " ", result.stdout)
    assert re.search(re.escape(contract) + r" ?BROKEN", flat), result.stdout
