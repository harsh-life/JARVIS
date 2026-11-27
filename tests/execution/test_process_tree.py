"""Process-tree cleanup, output accounting, mandatory limits and the launcher
(server/execution/process.py, server/execution/launcher.py).

Every test runs real processes and checks the **descendants themselves** — not
that a coroutine returned. Each runs on both paths (confined, which needs
Landlock, and break-glass), like `test_process.py`.

| Fix | Tests |
|---|---|
| a parent exiting first no longer stops the cleanup | `test_a_parent_that_exits_first_does_not_leave_its_child`, `test_a_child_holding_the_output_pipes_does_not_stall_the_call`, `test_a_child_with_closed_output_is_still_ended` |
| SIGTERM → SIGKILL, on every way a call ends | `test_a_descendant_that_ignores_sigterm_is_killed`, `test_timeout_ends_every_descendant`, `test_cancellation_ends_every_descendant`, `test_an_exception_mid_run_still_ends_the_tree`, `test_repeated_runs_leave_no_orphans` |
| no `preexec_fn`; concurrency from threads | `test_no_preexec_fn_anywhere_in_the_server`, `test_concurrent_spawns_from_a_busy_threaded_server` |
| mandatory limits fail closed | `test_*_limit*`, `test_a_restriction_that_cannot_be_applied_runs_nothing`, `test_the_launcher_refuses_a_malformed_spec` |
| combined output budget, honest metadata | `test_output_*` |
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from server.execution import confinement, launcher
from server.execution.process import LAUNCHER_PATH, MANDATORY_RLIMITS, ConstrainedProcessExecutor
from shared.schemas.execution import ExecutionError, ExecutionErrorCode
from tests.execution.test_process import execution_path, executor  # noqa: F401 — the both-paths fixture

REPO = Path(__file__).resolve().parents[2]
LINUX = sys.platform.startswith("linux")


def alive(pid: int) -> bool:
    """A live, non-zombie process (zombies are dead for every purpose here)."""

    if LINUX:
        try:
            with open(f"/proc/{pid}/stat") as handle:
                return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
        except (OSError, IndexError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


async def eventually_dead(pid: int, seconds: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not alive(pid):
            return True
        await asyncio.sleep(0.05)
    return not alive(pid)


def pids(text: str) -> list[int]:
    return [int(x) for x in text.split() if x.isdigit()]


async def wait_for_file(path: Path, seconds: float = 5.0) -> str:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        await asyncio.sleep(0.02)
    raise AssertionError(f"{path} never appeared — test setup is broken")


# ── the parent-exits-first path ───────────────────────────────────────────


async def test_a_parent_that_exits_first_does_not_leave_its_child(tmp_path):
    ex = executor(allowed_executables=["sh"])
    started = time.monotonic()
    result = await ex.run(["sh", "-c", "sleep 30 & echo $!"], cwd=str(tmp_path), timeout=10)
    assert time.monotonic() - started < 5          # not held to the wall clock by the child
    assert result.metadata["exit_code"] == 0 and result.metadata["timed_out"] is False
    [child] = pids(result.content)
    assert await eventually_dead(child), "the background child outlived its parent's call"
    if LINUX:
        assert result.metadata["cleanup"] == "verified"


async def test_parent_and_child_completing_normally_report_normally(tmp_path):
    ex = executor(allowed_executables=["sh"])
    result = await ex.run(["sh", "-c", "sleep 0.2 & wait; echo done"], cwd=str(tmp_path))
    assert result.content.strip() == "done"
    assert (result.metadata["exit_code"], result.metadata["timed_out"], result.metadata["truncated"]) == (0, False, False)
    if LINUX:
        assert result.metadata["cleanup"] == "verified"


async def test_a_child_holding_the_output_pipes_does_not_stall_the_call(tmp_path):
    ex = executor(allowed_executables=["sh"])
    marker = tmp_path / "child.pid"
    started = time.monotonic()
    result = await ex.run(["sh", "-c", f"sleep 30 & echo $! > {marker}; echo parent-done"], cwd=str(tmp_path),
                          timeout=8)
    assert time.monotonic() - started < 5
    assert result.content.strip() == "parent-done" and result.metadata["timed_out"] is False
    assert await eventually_dead(int(marker.read_text()))


async def test_a_child_with_closed_output_is_still_ended(tmp_path):
    ex = executor(allowed_executables=["sh"])
    result = await ex.run(["sh", "-c", "sleep 30 </dev/null >/dev/null 2>&1 & echo $!"], cwd=str(tmp_path))
    [child] = pids(result.content)
    assert await eventually_dead(child), "a child that closed its pipes escaped the cleanup"


async def test_a_descendant_that_ignores_sigterm_is_killed(tmp_path):
    ex = executor(allowed_executables=["sh"])
    marker = tmp_path / "stubborn.pid"
    result = await ex.run(
        ["sh", "-c", f"(trap '' TERM; exec sleep 30) & echo $! > {marker}; wait"],
        cwd=str(tmp_path), timeout=3.0)
    assert result.metadata["timed_out"] is True
    assert await eventually_dead(int(marker.read_text()), 1.0), "SIGTERM-ignoring descendant survived"


async def test_timeout_ends_every_descendant(tmp_path):
    ex = executor(allowed_executables=["sh"])
    marker = tmp_path / "tree.pids"
    result = await ex.run(
        ["sh", "-c", f"sleep 30 & a=$!; (sleep 30 & echo $! >> {marker}; wait) & echo $a >> {marker}; wait"],
        cwd=str(tmp_path), timeout=3.0)
    assert result.metadata["timed_out"] is True and result.metadata["exit_code"] == -1
    for pid in pids(marker.read_text()):
        assert await eventually_dead(pid, 1.0)


async def test_cancellation_ends_every_descendant(tmp_path):
    ex = executor(allowed_executables=["sh"])
    marker = tmp_path / "cancel.pid"
    running = asyncio.ensure_future(
        ex.run(["sh", "-c", f"sleep 30 & echo $! > {marker}; wait"], cwd=str(tmp_path), timeout=10))
    child = int(await wait_for_file(marker))
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert not alive(child), "the descendant outlived the cancelled call"


async def test_an_exception_mid_run_still_ends_the_tree(tmp_path, monkeypatch):
    ex = executor(allowed_executables=["sh"])
    marker = tmp_path / "boom.pid"
    original = ConstrainedProcessExecutor._communicate_bounded

    async def explode(self, tree, deadline):
        await wait_for_file(marker)
        raise RuntimeError("an unexpected failure while the tree is running")

    monkeypatch.setattr(ConstrainedProcessExecutor, "_communicate_bounded", explode)
    with pytest.raises(RuntimeError):
        await ex.run(["sh", "-c", f"sleep 30 & echo $! > {marker}; wait"], cwd=str(tmp_path), timeout=10)
    monkeypatch.setattr(ConstrainedProcessExecutor, "_communicate_bounded", original)
    assert not alive(int(marker.read_text()))


async def test_repeated_runs_leave_no_orphans(tmp_path):
    ex = executor(allowed_executables=["sh"])
    children = []
    for _ in range(15):
        result = await ex.run(["sh", "-c", "sleep 30 & sleep 30 & echo $!"], cwd=str(tmp_path))
        children += pids(result.content)
    assert len(children) == 15
    for pid in children:
        assert await eventually_dead(pid)


# ── no Python between fork and exec ───────────────────────────────────────


def test_no_preexec_fn_anywhere_in_the_server():
    offenders = []
    for path in (REPO / "server").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.keyword) and node.arg == "preexec_fn":
                offenders.append(path.relative_to(REPO).as_posix())
    assert offenders == []


async def test_concurrent_spawns_from_a_busy_threaded_server(tmp_path):
    """A `preexec_fn` could deadlock a child forked while another thread held
    a lock. Many spawns, concurrently, while threads churn logging and the
    allocator: every one completes, with its own output."""

    stop = threading.Event()
    log = logging.getLogger("hypermind.test.churn")

    def churn() -> None:
        # Take and release the locks a forked child would inherit held
        # (logging's, the allocator's, the import lock's) as often as possible
        # without monopolising the GIL — the race is about *held locks at fork
        # time*, not CPU starvation.
        while not stop.is_set():
            log.debug("x" * 64)
            _ = [bytearray(4096) for _ in range(32)]
            __import__("json")
            time.sleep(0)

    threads = [threading.Thread(target=churn, daemon=True) for _ in range(8)]
    for t in threads:
        t.start()
    try:
        ex = executor(allowed_executables=["sh"])
        results = await asyncio.wait_for(asyncio.gather(*[
            ex.run(["sh", "-c", f"echo run-{i}"], cwd=str(tmp_path), timeout=20) for i in range(40)
        ]), timeout=120)
    finally:
        stop.set()
        for t in threads:
            t.join(5)
    assert [r.content.strip() for r in results] == [f"run-{i}" for i in range(40)]
    assert [r.metadata["exit_code"] for r in results] == [0] * 40, [
        (r.metadata["exit_code"], r.metadata["stderr"][:200], r.metadata["limits"]) for r in results
        if r.metadata["exit_code"]]


# ── mandatory limits fail closed; the reported status is the real one ─────


async def test_mandatory_limits_are_reported_enforced(tmp_path):
    ex = executor(allowed_executables=["echo"])
    result = await ex.run(["echo", "hi"], cwd=str(tmp_path))
    limits = result.metadata["limits"]
    assert all(limits[name] == "enforced" for name in MANDATORY_RLIMITS)
    assert set(limits) == {"cpu", "as", "nofile", "fsize", "nproc"}
    assert result.metadata["aggregate_limits"] == "not_enforced"   # rlimits are per process


class FakeLimits:
    """Stands in for the kernel's rlimit table, so these unit tests never lower
    the test runner's own limits."""

    def __init__(self, refuse=()) -> None:
        self.values: dict[int, tuple[int, int]] = {}
        self.refuse = set(refuse)

    def getrlimit(self, limit):
        return self.values.get(limit, (launcher.resource.RLIM_INFINITY, launcher.resource.RLIM_INFINITY))

    def setrlimit(self, limit, value):
        if limit in self.refuse:
            raise OSError("refused by this host")
        self.values[limit] = tuple(value)


def test_a_mandatory_limit_that_cannot_be_set_refuses(monkeypatch):
    fake = FakeLimits(refuse={launcher.resource.RLIMIT_CPU})
    monkeypatch.setattr(launcher.resource, "getrlimit", fake.getrlimit)
    monkeypatch.setattr(launcher.resource, "setrlimit", fake.setrlimit)
    with pytest.raises(launcher.Refused) as excinfo:
        launcher.apply_rlimits({"cpu": 10, "nproc": 100}, ["cpu"])
    assert excinfo.value.code == "rlimit_failed"


def test_an_optional_limit_that_cannot_be_set_is_reported_unavailable(monkeypatch):
    fake = FakeLimits(refuse={getattr(launcher.resource, "RLIMIT_NPROC", -1)})
    monkeypatch.setattr(launcher.resource, "getrlimit", fake.getrlimit)
    monkeypatch.setattr(launcher.resource, "setrlimit", fake.setrlimit)
    status = launcher.apply_rlimits({"nofile": 64, "nproc": 100}, ["nofile"])
    assert status == {"nofile": "enforced", "nproc": "unavailable"}
    assert fake.values[launcher.resource.RLIMIT_NOFILE] == (64, 64)


def test_a_limit_the_kernel_silently_ignores_is_not_reported_enforced(monkeypatch):
    monkeypatch.setattr(launcher.resource, "setrlimit", lambda limit, value: None)  # pretends success
    with pytest.raises(launcher.Refused):
        launcher.apply_rlimits({"fsize": 1024}, ["fsize"])


def test_a_limit_above_the_hard_ceiling_is_lowered_never_raised():
    soft, hard = launcher.resource.getrlimit(launcher.resource.RLIMIT_NOFILE)
    if hard == launcher.resource.RLIM_INFINITY:
        pytest.skip("no finite hard limit to compare against")
    code = (f"import sys; sys.path.insert(0, {str(LAUNCHER_PATH.parent)!r}); import launcher, resource; "
            f"print(launcher.apply_rlimits({{'nofile': {hard * 4}}}, ['nofile']), resource.getrlimit(resource.RLIMIT_NOFILE))")
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True).stdout
    assert f"({hard}, {hard})" in out and "enforced" in out


def _run_launcher(spec, extra_fds=()) -> tuple[int, list[dict]]:
    spec_r, spec_w = os.pipe()
    status_r, status_w = os.pipe()
    proc = subprocess.Popen([sys.executable, "-I", "-S", "-B", str(LAUNCHER_PATH), "--spec-fd", str(spec_r),
                             "--status-fd", str(status_w)], pass_fds=(spec_r, status_w, *extra_fds), env={})
    os.close(spec_r)
    os.close(status_w)
    os.write(spec_w, json.dumps(spec).encode())
    os.close(spec_w)
    data = b""
    while chunk := os.read(status_r, 65536):
        data += chunk
    os.close(status_r)
    return proc.wait(10), [json.loads(line) for line in data.splitlines()]


@pytest.mark.parametrize("mutation", [
    {"target": "relative/path"},
    {"argv": ["/bin/echo-not-the-target", "x"]},
    {"argv": []},
    {"env": {"A": 1}},
    {"rlimits": {"cpu": -1}},
    {"rlimits": {"stack": 1}},
    {"mandatory": ["fsize"]},
    {"ruleset_fd": 7},                              # landlock without seccomp
    {"extra": True},
])
async def test_the_launcher_refuses_a_malformed_spec(tmp_path, mutation):
    marker = tmp_path / "ran"
    spec = {"target": "/bin/sh", "argv": ["/bin/sh", "-c", f"touch {marker}"], "env": {},
            "rlimits": {"cpu": 5}, "mandatory": ["cpu"], "ruleset_fd": None, "seccomp_filter": None, **mutation}
    code, records = _run_launcher(spec)
    assert code == launcher.EXIT_REFUSED and records[0]["error"] == "invalid_spec"
    assert not marker.exists()


async def test_the_launcher_reports_what_it_enforced_and_execs(tmp_path):
    marker = tmp_path / "ran"
    code, records = _run_launcher({"target": "/bin/sh", "argv": ["/bin/sh", "-c", f"touch {marker}"], "env": {},
                                   "rlimits": {"cpu": 5, "nofile": 64}, "mandatory": ["cpu", "nofile"],
                                   "ruleset_fd": None, "seccomp_filter": None})
    assert code == 0 and marker.exists()
    assert records == [{"ready": True, "limits": {"cpu": "enforced", "nofile": "enforced"},
                        "kernel_confinement": False}]


@pytest.mark.skipif(not confinement.available(), reason="needs Landlock")
async def test_a_restriction_that_cannot_be_applied_runs_nothing(tmp_path, monkeypatch):
    """If Landlock cannot be applied in the child, the target never runs."""

    marker = tmp_path / "ran"
    real_build = confinement.build_plan

    def broken_plan(**kwargs):
        plan = real_build(**kwargs)
        os.close(plan.ruleset_fd)
        plan.ruleset_fd = os.open("/dev/null", os.O_RDONLY)   # not a Landlock ruleset
        return plan

    monkeypatch.setattr(confinement, "build_plan", broken_plan)
    ex = ConstrainedProcessExecutor(allowed_executables=["sh"])
    with pytest.raises(ExecutionError) as excinfo:
        await ex.run(["sh", "-c", f"touch {marker}"], cwd=str(tmp_path))
    assert excinfo.value.code is ExecutionErrorCode.SANDBOX_VIOLATION
    assert "nothing was run" in str(excinfo.value)
    assert not marker.exists()


# ── output: one combined budget, deterministic split, honest metadata ─────


def _py(code: str) -> list[str]:
    return ["python3", "-c", code]


async def test_output_stdout_only_overflow(tmp_path):
    ex = executor(allowed_executables=["python3"], max_output_bytes=4000)
    r = await ex.run(_py("import sys; sys.stdout.write('o' * 100000)"), cwd=str(tmp_path))
    m = r.metadata
    assert len(r.content) == m["stdout_limit_bytes"] == 3000 and m["stderr"] == ""
    assert (m["stdout_truncated"], m["stderr_truncated"], m["truncated"], m["timed_out"]) == (True, False, True, False)


async def test_output_stderr_only_overflow(tmp_path):
    ex = executor(allowed_executables=["python3"], max_output_bytes=4000)
    r = await ex.run(_py("import sys; sys.stderr.write('e' * 100000)"), cwd=str(tmp_path))
    m = r.metadata
    assert r.content == "" and len(m["stderr"]) == m["stderr_limit_bytes"] == 1000
    assert (m["stdout_truncated"], m["stderr_truncated"], m["truncated"]) == (False, True, True)


async def test_output_simultaneous_overflow_stays_within_the_combined_budget(tmp_path):
    ex = executor(allowed_executables=["python3"], max_output_bytes=4000)
    r = await ex.run(_py("import sys\nfor _ in range(200):\n sys.stdout.write('o'*1000); sys.stderr.write('e'*1000)"),
                     cwd=str(tmp_path))
    m = r.metadata
    assert len(r.content) + len(m["stderr"]) <= m["output_limit_bytes"] == 4000
    assert m["stdout_truncated"] and m["stderr_truncated"] and m["exit_code"] == 0


async def test_output_large_continuous_output_is_drained_not_deadlocked(tmp_path):
    ex = executor(allowed_executables=["python3"], max_output_bytes=1000)
    started = time.monotonic()
    r = await ex.run(_py("import sys\nb = b'z' * 65536\nfor _ in range(800): sys.stdout.buffer.write(b)"),
                     cwd=str(tmp_path), timeout=20)   # ~50 MiB through a pipe the reader never keeps
    assert r.metadata["exit_code"] == 0 and r.metadata["timed_out"] is False
    assert len(r.content) <= 1000 and time.monotonic() - started < 20


async def test_output_timeout_during_output(tmp_path):
    ex = executor(allowed_executables=["python3"], max_output_bytes=2000)
    r = await ex.run(_py("import sys\nwhile True: sys.stdout.write('t' * 4096); sys.stdout.flush()"),
                     cwd=str(tmp_path), timeout=1.0)
    m = r.metadata
    assert (m["timed_out"], m["exit_code"], m["stdout_truncated"]) == (True, -1, True)
    assert len(r.content) <= m["stdout_limit_bytes"]


async def test_output_cancellation_during_output_ends_the_process(tmp_path):
    ex = executor(allowed_executables=["sh"], max_output_bytes=2000)
    marker = tmp_path / "writer.pid"
    running = asyncio.ensure_future(ex.run(
        ["sh", "-c", f"echo $$ > {marker}; while :; do echo spam; done"], cwd=str(tmp_path), timeout=10))
    writer = int(await wait_for_file(marker))
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert not alive(writer)


async def test_output_normal_completion_metadata(tmp_path):
    ex = executor(allowed_executables=["sh"])
    r = await ex.run(["sh", "-c", "echo out; echo err >&2; exit 3"], cwd=str(tmp_path))
    m = r.metadata
    assert (r.content, m["stderr"], m["exit_code"]) == ("out\n", "err\n", 3)
    assert (m["truncated"], m["stdout_truncated"], m["stderr_truncated"], m["timed_out"]) == (False, False, False, False)
