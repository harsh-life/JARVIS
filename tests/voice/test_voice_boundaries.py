"""The voice package's module boundary (docs/27 §0, 16 §6, VOI-T3/T4).

Voice produces text and audio, nothing more. The contracts that keep it away
from identity, authorization, confirmation and step-up, secrets, memory,
execution and the scheduler are declared, kept, and each would catch a
deliberate violation.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

CONTRACTS = (
    "Voice is an input method, not an identity or an authority (docs/27, VOI-T3/T4)",
    "Voice opens no process or socket (docs/27, REPO-T5)",
)


def _lint(config: Path, cwd: Path) -> subprocess.CompletedProcess:
    script = Path(sys.executable).parent / "lint-imports"
    return subprocess.run([str(script), "--config", str(config)], cwd=cwd, capture_output=True, text=True)



def test_every_voice_contract_is_declared_and_kept():
    result = _lint(REPO / "pyproject.toml", REPO)
    assert result.returncode == 0, result.stdout
    flat = re.sub(r"\s+", " ", result.stdout)
    for name in CONTRACTS:
        assert re.search(re.escape(name) + r" KEPT", flat), name
    contracts = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["importlinter"]["contracts"]
    superuser = next(c for c in contracts if c["name"].startswith("Only the gateway reaches superuser authority"))
    assert "server.voice" in superuser["source_modules"]


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
    chosen = [c for c in contracts if c["name"] in CONTRACTS]
    lines = ["[tool.importlinter]", 'root_packages = ["server", "shared"]', "include_external_packages = true", ""]
    for contract in chosen:
        lines.append("[[tool.importlinter.contracts]]")
        for key, value in contract.items():
            lines.append(f"{key} = {value!r}".replace("'", '"').replace("True", "true").replace("False", "false"))
        lines.append("")
    (root / "fixture.toml").write_text("\n".join(lines))
    return root


@pytest.mark.parametrize("violation", [
    ("server/voice/leak.py", "server.auth"),
    ("server/voice/leak.py", "server.capabilities"),
    ("server/voice/leak.py", "server.graph"),
    ("server/voice/leak.py", "server.secrets.store"),
    ("server/voice/leak.py", "server.memory"),
    ("server/voice/leak.py", "server.vault"),
    ("server/voice/leak.py", "server.agent"),
    ("server/voice/leak.py", "server.execution"),
    ("server/voice/leak.py", "server.scheduler"),
    ("server/voice/leak.py", "server.composition"),
    ("server/voice/leak.py", "socket"),
])
def test_each_voice_contract_detects_a_deliberate_violation(tmp_path, violation):
    clean = _fixture_project(tmp_path / "clean", ("server/voice/ok.py", "server.security.audit"))
    assert _lint(clean / "fixture.toml", clean).returncode == 0, _lint(clean / "fixture.toml", clean).stdout
    root = _fixture_project(tmp_path, violation)
    result = _lint(root / "fixture.toml", root)
    assert result.returncode != 0 and "BROKEN" in result.stdout, result.stdout
