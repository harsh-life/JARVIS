"""Module-boundary (import-linter) tests.

16_REPOSITORY_MODULE_BOUNDARIES.md §6 [LOCKED]: "a boundary violation ...
is a CI failure, not a review nicety." §9 acceptance criterion:
"Dependency direction is mechanically testable."

Two angles, both required:
  1. the real repo, as it stands, satisfies its own declared contracts
     (pyproject.toml [[tool.importlinter.contracts]]).
  2. import-linter genuinely *detects* a violation — proven against an
     isolated throwaway fixture project, not by mutating this repo.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]


def _lint_imports_cmd() -> list[str]:
    """The `lint-imports` console script installed alongside this
    interpreter (it is a click Command object, not a `python -m` submodule
    — `python -m importlinter.cli lint-imports` silently does nothing)."""

    script = Path(sys.executable).parent / "lint-imports"
    assert script.exists(), f"lint-imports console script not found at {script}"
    return [str(script)]


def test_real_repo_satisfies_its_own_module_boundary_contracts() -> None:
    result = subprocess.run(
        [*_lint_imports_cmd(), "--config", "pyproject.toml"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Contracts: 4 kept, 0 broken." in result.stdout


def test_import_linter_detects_a_deliberate_violation(tmp_path: Path) -> None:
    """REPO-T7 mechanism test: build an isolated two-module fixture where
    `foo` is forbidden from importing `bar`, have `foo` import `bar`
    anyway, and confirm import-linter's own exit code/report catch it.
    This does not touch the real repository's source tree.
    """

    project = tmp_path / "boundary_fixture"
    (project / "foo").mkdir(parents=True)
    (project / "bar").mkdir(parents=True)
    (project / "foo" / "__init__.py").write_text("from bar import something\n")
    (project / "bar" / "__init__.py").write_text("something = 1\n")

    config = project / "fixture_importlinter.toml"
    config.write_text(
        textwrap.dedent(
            """
            [tool.importlinter]
            root_packages = ["foo", "bar"]

            [[tool.importlinter.contracts]]
            name = "foo must never import bar"
            type = "forbidden"
            source_modules = ["foo"]
            forbidden_modules = ["bar"]
            """
        )
    )

    result = subprocess.run(
        [*_lint_imports_cmd(), "--config", str(config)],
        cwd=project,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "import-linter should have failed on a deliberate violation"
    assert "BROKEN" in result.stdout
