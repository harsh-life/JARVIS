"""BR-T2, re-measured after Runtime + Execution integration (14 §4, 17 §4).

`docs/OD_A1_BR_T2.md` §4 left the filesystem (`09`) and egress (`10`)
dimensions PENDING "until they exist", and required a re-run when they did.
They exist now, and so does a third surface the original measurement could not
see: the `system.restricted` process executor. This module is that re-run.

Two attacker models are measured and kept apart, because the owner's OD-A1
acceptance covers only one of them:

* **app-RCE** — code running *inside* the server process (the class the owner
  accepted: "a compromised live application process may reach data and secrets
  that are already available to that running process").
* **authorized** — an ordinary authenticated user driving the agent through
  paths the deterministic layer *allows*, including a human-confirmed,
  step-up-fresh `system.restricted` call. Anything reachable this way is **not**
  inside the accepted class; it is a cross-user authorization failure.

Like the original, it asserts the measurement in both directions, so a
documented residual can never silently turn into a false isolation claim
(INV-20), and a closed path cannot quietly reopen.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from server.composition.break_glass import BreakGlassRegistry
from server.config.schema import BreakGlassConfig
from server.execution import confinement
from server.execution.process import ConstrainedProcessExecutor
from server.fs import FilesystemSandbox
from server.tools.platforms import FileReadAdapter
from shared.schemas.agent import ExecutionPlatform, ToolInvocation
from shared.schemas.execution import ExecutionError
from tests.support import make_superuser

pytestmark = pytest.mark.asyncio

B_TEXT = "TEST-ONLY user B's private sandbox text"


@dataclass
class Row:
    attempt: str
    model: str  # "app-RCE" | "authorized"
    reachable: bool | None  # None = not measurable on this host
    detail: str

    def render(self) -> str:
        mark = "NOT MEASURED" if self.reachable is None else ("REACHABLE" if self.reachable else "contained")
        return f"  [{mark:>12}] ({self.model}) {self.attempt} — {self.detail}"


def _b_file(sandbox: FilesystemSandbox, user_b: uuid.UUID) -> Path:
    root = sandbox.root_for(user_id=user_b, graph_id=None, label="notes")
    sandbox.create_file(root, "diary.txt", B_TEXT)
    return Path(root) / "diary.txt"


async def test_br_t2_execution_dimensions(tmp_path, capsys, monkeypatch):
    superuser = make_superuser(monkeypatch)
    sandbox = FilesystemSandbox(base_root=str(tmp_path / "sandboxes"))
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    b_path = _b_file(sandbox, user_b)
    task_temp = sandbox.task_temp(task_id=uuid.uuid4())
    rows: list[Row] = []

    # ── 12. B's file through A's own file tool ───────────────────────────────
    reader = FileReadAdapter(sandbox)
    out = await reader.execute(ToolInvocation(
        tool_id="files.read", operation="read_file",
        arguments={"relative_path": f"../../../{user_b}/private/notes/diary.txt"},
        user_id=user_a, task_id=uuid.uuid4(), platform=ExecutionPlatform.SERVER,
        resource_scope={"sandbox_root": "notes"}, device_id=uuid.uuid4(),
    ))
    rows.append(Row("read B's sandbox file through A's file tool", "authorized",
                    B_TEXT in out.content,
                    "roots are derived from the server-side user id; '..' is refused (09 §1/§2)"))

    # ── 13. B's file by opening it directly in-process ───────────────────────
    try:
        direct = b_path.read_text()
    except OSError:
        direct = ""
    rows.append(Row("read B's sandbox file by opening its path in-process", "app-RCE",
                    direct == B_TEXT,
                    "`mediated` containment is application code; the process owns the files (09 §8)"))

    # ── 14. A raw socket in-process, around server.net ───────────────────────
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        conn = socket.create_connection(listener.getsockname(), timeout=2)
        conn.close()
        raw_socket = True
    except OSError:
        raw_socket = False
    finally:
        listener.close()
    rows.append(Row("open a raw socket to an undeclared destination in-process", "app-RCE",
                    raw_socket,
                    "`mediated_proxy` binds this codebase's adapters, not the process (10 §3)"))

    # ── 15/16. An approved system.restricted process, confined (the default) ─
    if confinement.available():
        confined = ConstrainedProcessExecutor(allowed_executables=["cat"])
        b_read = await confined.run(["cat", str(b_path)], cwd=task_temp)
        env_read = await confined.run(["cat", f"/proc/{os.getpid()}/environ"], cwd=task_temp)
        rows.append(Row("read B's sandbox file from an approved shell command", "authorized",
                        B_TEXT in b_read.content, "Landlock: no reads outside system dirs + task temp"))
        rows.append(Row("read the server's environment (env: KEK, superuser token) from an approved shell command",
                        "authorized", bool(env_read.content), "Landlock: /proc is outside the ruleset"))
    else:
        for attempt in ("read B's sandbox file from an approved shell command",
                        "read the server's environment (env: KEK, superuser token) from an approved shell command"):
            rows.append(Row(attempt, "authorized", None,
                            "no Landlock on this host — `landlock` mode refuses to run anything here"))

    # ── 17. The same command under break-glass (20 §2, BG-T10) ───────────────
    # The global `unconfined` opt-out is gone. What replaced it is re-measured
    # here with a real record store and a real superuser activation: inside an
    # active window the command reaches B's data and the server's environment
    # — OD-A1's residual, reached on purpose — and outside one it does not.
    registry = BreakGlassRegistry(BreakGlassConfig(enabled=True, allowed_executables=["cat"], max_invocations=2))
    loose = ConstrainedProcessExecutor(break_glass_executables=["cat"], break_glass=registry)
    task_a, task_other = uuid.uuid4(), uuid.uuid4()

    async def attempt(argv: list[str], task_id: uuid.UUID) -> bool:
        try:
            result = await loose.run(argv, cwd=task_temp, task_id=task_id, user_id=user_a)
        except ExecutionError:
            return False
        return bool(result.content)

    rows.append(Row("read B's sandbox file from an approved shell command, break-glass enabled, no record",
                    "authorized", await attempt(["cat", str(b_path)], task_a),
                    "enablement alone runs nothing unconfined (20 §2.2 key one of two)"))
    record = registry.prepare(superuser, task_id=task_a, task_owner=user_a, user_id=user_a, executables=["cat"],
                              max_invocations=2, window_seconds=60, task_seconds_left=120, reason="br_t2")
    registry.install(superuser, record)
    rows.append(Row("read B's sandbox file from another task's approved shell command, break-glass active",
                    "authorized", await attempt(["cat", str(b_path)], task_other),
                    "a record binds one task"))
    rows.append(Row("read B's sandbox file from an approved shell command, break-glass active",
                    "authorized", B_TEXT in (await loose.run(["cat", str(b_path)], cwd=task_temp,
                                                             task_id=task_a, user_id=user_a)).content,
                    "OD-A1 residual, reached deliberately: the record removes the kernel layer (20 §4)"))
    rows.append(Row("read the server's environment (env: KEK, superuser token), break-glass active",
                    "authorized", await attempt(["cat", f"/proc/{os.getpid()}/environ"], task_a),
                    "same: every activation is a cross-user exposure event (20 §4)"))
    rows.append(Row("read B's sandbox file from an approved shell command, break-glass invocations spent",
                    "authorized", await attempt(["cat", str(b_path)], task_a),
                    "max_invocations=2 used: the record has ended"))

    print("\nBR-T2 (execution dimensions) measured blast radius:")
    for row in rows:
        print(row.render())

    measured = {row.attempt: row.reachable for row in rows}

    # Contained: authorized paths never reach another user's data or the
    # server's own secrets. A failure here is a cross-user bypass, not a residual.
    assert measured["read B's sandbox file through A's file tool"] is False
    for attempt in ("read B's sandbox file from an approved shell command",
                    "read the server's environment (env: KEK, superuser token) from an approved shell command"):
        assert measured[attempt] in (False, None), attempt

    # Contained: break-glass outside its one live window.
    for contained in ("read B's sandbox file from an approved shell command, break-glass enabled, no record",
                      "read B's sandbox file from another task's approved shell command, break-glass active",
                      "read B's sandbox file from an approved shell command, break-glass invocations spent"):
        assert measured[contained] is False, contained

    # Reachable, and asserted so: the accepted app-RCE class (OD-A1 (a)), and
    # break-glass inside its window — recorded, never claimed closed (BG-T10).
    # If one of these starts failing, a boundary improved and
    # docs/OD_A1_BR_T2.md must be updated — not the assertion deleted.
    assert measured["read B's sandbox file by opening its path in-process"] is True
    assert measured["open a raw socket to an undeclared destination in-process"] is True
    assert measured["read B's sandbox file from an approved shell command, break-glass active"] is True
    assert measured["read the server's environment (env: KEK, superuser token), break-glass active"] is True
