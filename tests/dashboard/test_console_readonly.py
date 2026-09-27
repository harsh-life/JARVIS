"""DASH-002 / DSH-T1 at the module level: `server/dashboard` has no mutation path.

The import contract ("The dashboard is read-only") keeps the package from
reaching anything that acts. This checks the package's own code: no SQL
`insert`/`update`/`delete`, and no session write (`add`, `delete`, `merge`,
`flush`, `commit`) anywhere in it — so even a route that wanted to write
through the session it is handed would have nothing to call.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[2] / "server" / "dashboard"
WRITE_CALLS = {"add", "add_all", "delete", "merge", "flush", "commit", "execute_write", "begin_nested"}
SQL_WRITES = {"insert", "update", "delete", "Insert", "Update", "Delete"}


def _modules() -> list[Path]:
    return sorted(DASHBOARD.glob("*.py"))


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_no_dashboard_module_can_write(path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("sqlalchemy"):
            assert not {a.name for a in node.names} & SQL_WRITES, (path.name, node.module)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in WRITE_CALLS, (path.name, node.lineno, node.func.attr)


def test_the_package_is_not_empty():
    names = {p.name for p in _modules()}
    assert {"console.py", "ports.py", "redaction.py"} <= names


def test_the_check_would_catch_a_write(tmp_path):
    bad = "async def v(session):\n    session.add(object())\n    await session.commit()\n"
    tree = ast.parse(bad)
    hits = [n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    assert set(hits) & WRITE_CALLS == {"add", "commit"}
