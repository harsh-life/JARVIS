"""SCH-T2 (static half) and the scheduler's module boundary (docs/22 §0, 16 §6).

The runtime half of SCH-T2 is `test_firing.py::test_sch_t2_…`: a real firing,
with the runtime, tools, device dispatch, engine and confirmations trapped.
This half proves the same thing about the *code*: the scheduler package cannot
import anything that acts, and the contracts that say so would catch a
deliberate violation.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCHEDULER = REPO / "server" / "scheduler"

CONTRACTS = (
    "The scheduler never executes: no runtime, tool, device, authority, secret, memory or judge (docs/22, SCH-T2)",
    "The scheduler opens no process or socket (docs/22, REPO-T5)",
    "Only the gateway reaches superuser authority (12 §4, SUPER-001)",
    "Memory/vault reach no control, device, scheduler, voice or judge code (docs/21)",
)

# What server/scheduler may import from inside the repository: storage, the
# audit/usage half of security, config, its own modules, and shared schemas.
ALLOWED_INTERNAL = re.compile(
    r"^(server\.scheduler(\..*)?|server\.storage(\..*)?|server\.config(\..*)?|"
    r"server\.security\.(audit|events|usage)|shared\.schemas(\..*)?)$"
)


def _lint(config: Path, cwd: Path) -> subprocess.CompletedProcess:
    script = Path(sys.executable).parent / "lint-imports"
    return subprocess.run([str(script), "--config", str(config)], cwd=cwd, capture_output=True, text=True)


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_scheduler_imports_only_storage_audit_config_and_schemas():
    for path in sorted(SCHEDULER.glob("*.py")):
        for name in _imports(path):
            if name.startswith(("server.", "shared.")):
                assert ALLOWED_INTERNAL.match(name), f"{path.name} imports {name}"
            assert name.split(".")[0] not in {"subprocess", "socket", "httpx", "requests", "urllib"}, (path, name)


def test_the_scheduler_never_names_the_runtime_or_an_operation():
    """Belt and braces for SCH-T2: no call site in the package refers to task
    submission, tool running, device operations, confirmation or grants."""

    forbidden = {"submit", "run_tool", "run", "send", "build_operation", "confirm", "issue", "grant",
                 "create_grant", "authorize", "execute", "activate"}
    for path in sorted(SCHEDULER.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                # `session.execute` is SQL, not a tool.
                if name == "execute" and isinstance(func, ast.Attribute) and getattr(func.value, "id", "") == "session":
                    continue
                assert name not in forbidden, f"{path.name}:{node.lineno} calls {name}()"


def test_every_scheduler_contract_is_declared_and_kept():
    result = _lint(REPO / "pyproject.toml", REPO)
    assert result.returncode == 0, result.stdout
    flat = re.sub(r"\s+", " ", result.stdout)
    for name in CONTRACTS:
        assert re.search(re.escape(name) + r" KEPT", flat), name
    contracts = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["importlinter"]["contracts"]
    superuser = next(c for c in contracts if c["name"] == CONTRACTS[2])
    assert "server.scheduler" in superuser["source_modules"]


def _fixture_project(tmp_path: Path, violation: tuple[str, str]) -> Path:
    root = tmp_path / "fixture"
    packages = [
        "server", "server/scheduler", "server/agent", "server/tools", "server/modeltools", "server/models",
        "server/execution", "server/fs", "server/net", "server/capabilities", "server/graph", "server/auth",
        "server/secrets", "server/memory", "server/vault", "server/evaluation", "server/intelligence",
        "server/voice", "server/dashboard", "server/gateway", "server/composition", "server/security",
        "server/storage", "server/config", "shared",
    ]
    for pkg in packages:
        (root / pkg).mkdir(parents=True, exist_ok=True)
        (root / pkg / "__init__.py").write_text("")
    for name in ("store", "kek", "crypto", "requester", "audit_port"):
        (root / f"server/secrets/{name}.py").write_text("")
    (root / "server/security/audit.py").write_text("import server.secrets.audit_port\n")
    source, target = violation
    (root / source).write_text(f"import {target}\n")

    contracts = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["importlinter"]["contracts"]
    chosen = [c for c in contracts if c["name"] in CONTRACTS[:2]]
    lines = ["[tool.importlinter]", 'root_packages = ["server", "shared"]', "include_external_packages = true", ""]
    for contract in chosen:
        lines.append("[[tool.importlinter.contracts]]")
        for key, value in contract.items():
            lines.append(f"{key} = {value!r}".replace("'", '"').replace("True", "true").replace("False", "false"))
        lines.append("")
    (root / "fixture.toml").write_text("\n".join(lines))
    return root


@pytest.mark.parametrize("violation", [
    ("server/scheduler/leak.py", "server.agent"),
    ("server/scheduler/leak.py", "server.tools"),
    ("server/scheduler/leak.py", "server.execution"),
    ("server/scheduler/leak.py", "server.capabilities"),
    ("server/scheduler/leak.py", "server.graph"),
    ("server/scheduler/leak.py", "server.secrets.store"),
    ("server/scheduler/leak.py", "server.memory"),
    ("server/scheduler/leak.py", "server.vault"),
    ("server/scheduler/leak.py", "server.evaluation"),
    ("server/scheduler/leak.py", "server.voice"),
    ("server/scheduler/leak.py", "server.composition"),
    ("server/scheduler/leak.py", "subprocess"),
    ("server/scheduler/leak.py", "socket"),
])
def test_each_scheduler_contract_detects_a_deliberate_violation(tmp_path, violation):
    clean = _fixture_project(tmp_path / "clean", ("server/scheduler/ok.py", "server.security.audit"))
    assert _lint(clean / "fixture.toml", clean).returncode == 0, _lint(clean / "fixture.toml", clean).stdout
    root = _fixture_project(tmp_path, violation)
    result = _lint(root / "fixture.toml", root)
    assert result.returncode != 0 and "BROKEN" in result.stdout, result.stdout
