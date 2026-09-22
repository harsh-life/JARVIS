"""server/execution/process.py — the `system.restricted` primitive.

Every test here runs a real subprocess (no mocking `asyncio.create_subprocess_
exec`) because the guarantee this module makes — no shell, no inherited host
environment, bounded output, whole-process-tree cleanup — is only real if
proven against an actual child process.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from server.execution.process import ConstrainedProcessExecutor
from shared.schemas.execution import ExecutionError, ExecutionErrorCode

pytestmark = pytest.mark.asyncio


def executor(**overrides) -> ConstrainedProcessExecutor:
    defaults = dict(
        allowed_executables=["echo", "sleep", "cat", "env", "python3", "sh", "bash"],
        default_timeout_seconds=5.0,
        max_timeout_seconds=10.0,
        max_output_bytes=1_000_000,
    )
    defaults.update(overrides)
    return ConstrainedProcessExecutor(**defaults)


# ── executable allow-list ────────────────────────────────────────────────


async def test_default_allowlist_is_empty_deny_all():
    ex = ConstrainedProcessExecutor()
    with pytest.raises(ExecutionError) as excinfo:
        await ex.run(["echo", "hi"], cwd="/tmp")
    assert excinfo.value.code == ExecutionErrorCode.UNAUTHORIZED_EXECUTABLE


async def test_unauthorized_executable_is_rejected():
    ex = executor(allowed_executables=["echo"])
    with pytest.raises(ExecutionError) as excinfo:
        await ex.run(["rm", "-rf", "/"], cwd="/tmp")
    assert excinfo.value.code == ExecutionErrorCode.UNAUTHORIZED_EXECUTABLE


async def test_allowlist_match_requires_exact_or_basename_not_substring():
    ex = executor(allowed_executables=["echo"])
    with pytest.raises(ExecutionError):
        await ex.run(["/bin/echoevil"], cwd="/tmp")


async def test_allowlisted_absolute_path_basename_is_accepted():
    ex = executor(allowed_executables=["echo"])
    result = await ex.run(["/bin/echo", "hi"], cwd="/tmp")
    assert result.metadata["exit_code"] == 0


# ── no shell, no injection ───────────────────────────────────────────────


async def test_shell_metacharacters_in_an_argument_are_inert():
    """Because argv is exec'd directly (never through a shell), a metachar
    string is just literal argv data to the child, not shell syntax."""

    ex = executor()
    payload = "hello; rm -rf / #`whoami`$(id)"
    result = await ex.run(["echo", payload], cwd="/tmp")
    assert payload in result.content
    assert "uid=" not in result.content  # $(id) was never evaluated


async def test_argv_element_with_nul_byte_is_rejected():
    ex = executor()
    with pytest.raises(ExecutionError) as excinfo:
        await ex.run(["echo", "a\x00b"], cwd="/tmp")
    assert excinfo.value.code == ExecutionErrorCode.INVALID_ARGUMENTS


async def test_empty_argv_is_rejected():
    ex = executor()
    with pytest.raises(ExecutionError):
        await ex.run([], cwd="/tmp")


async def test_source_never_constructs_a_shell_string():
    import ast
    import inspect

    from server.execution import process as process_module

    source = inspect.getsource(process_module)
    tree = ast.parse(source)
    # Strip the module docstring (which explains, in prose, exactly what this
    # test checks the *code* never does) before searching, so this test
    # checks behavior rather than banning the module's own explanation of it.
    if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant):
        code_only = "\n".join(source.splitlines()[tree.body[0].end_lineno:])
    else:
        code_only = source
    assert "shell=True" not in code_only
    assert "create_subprocess_shell" not in code_only


# ── environment sanitization ─────────────────────────────────────────────


async def test_host_environment_is_not_inherited(monkeypatch):
    monkeypatch.setenv("HYPERMIND_TEST_HOST_SECRET", "should-not-leak")
    ex = executor()
    result = await ex.run(["env"], cwd="/tmp")
    assert "HYPERMIND_TEST_HOST_SECRET" not in result.content
    assert "should-not-leak" not in result.content


async def test_credential_shaped_env_override_key_is_refused():
    ex = executor()
    with pytest.raises(ExecutionError) as excinfo:
        await ex.run(["env"], cwd="/tmp", env_overrides={"API_KEY": "sk-whatever"})
    assert excinfo.value.code == ExecutionErrorCode.INVALID_ARGUMENTS


async def test_safe_env_override_is_passed_through():
    ex = executor()
    result = await ex.run(["env"], cwd="/tmp", env_overrides={"HYPERMIND_TASK_LABEL": "unit-test"})
    assert "HYPERMIND_TASK_LABEL=unit-test" in result.content


# ── timeout / cancellation / process-tree cleanup ────────────────────────


async def test_timeout_kills_the_process_and_reports_it():
    ex = executor(default_timeout_seconds=1.0)
    started = time.monotonic()
    result = await ex.run(["sleep", "30"], cwd="/tmp", timeout=1.0)
    elapsed = time.monotonic() - started
    assert result.metadata["timed_out"] is True
    assert elapsed < 5.0  # nowhere near the full 30s sleep


def _process_is_running(pid: int) -> bool:
    """True only for a live, non-zombie process. `kill(pid, 0)` alone is not
    enough: a reaped-by-nobody child becomes a zombie and still answers
    `kill(pid, 0)` successfully (it still holds a pid-table entry) even
    though the kernel has already torn down everything about it that
    matters here — it is not "still running" in any sense this test cares
    about."""

    try:
        with open(f"/proc/{pid}/stat") as handle:
            # Field 3 (after the "(comm)" parenthesised part, which may itself
            # contain spaces/parens) is the state char; splitting on ')' and
            # taking the remainder sidesteps that.
            state = handle.read().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False


async def test_timeout_kills_the_whole_process_group_not_just_the_shell():
    """A child that forks a grandchild must not survive the parent's kill —
    the real process-tree-cleanup test, not just 'the direct pid died'."""

    ex = executor(allowed_executables=["sh"], default_timeout_seconds=1.0)
    marker = f"/tmp/hypermind_test_survivor_{os.getpid()}_{int(time.time()*1000)}"
    try:
        await ex.run(
            ["sh", "-c", f"sleep 20 & echo $! > {marker}; wait"],
            cwd="/tmp", timeout=1.0,
        )
        await asyncio.sleep(0.5)
        assert os.path.exists(marker), "the grandchild never started — test setup is broken"
        grandchild_pid = int(open(marker).read().strip())
        assert not _process_is_running(grandchild_pid), (
            "the grandchild survived the timeout kill — process-tree cleanup failed"
        )
    finally:
        if os.path.exists(marker):
            os.unlink(marker)


async def test_timeout_exceeding_max_is_clamped_not_rejected():
    ex = executor(default_timeout_seconds=1.0, max_timeout_seconds=1.0)
    result = await ex.run(["sleep", "5"], cwd="/tmp", timeout=9999.0)
    assert result.metadata["timed_out"] is True


async def test_nonpositive_timeout_is_rejected():
    ex = executor()
    with pytest.raises(ExecutionError):
        await ex.run(["echo", "hi"], cwd="/tmp", timeout=0)


# ── output capture limits ────────────────────────────────────────────────


async def test_output_over_cap_is_truncated_not_unbounded():
    ex = executor(allowed_executables=["python3"], max_output_bytes=1000)
    result = await ex.run(
        ["python3", "-c", "import sys; sys.stdout.write('A' * 1_000_000)"],
        cwd="/tmp",
    )
    assert len(result.content.encode("utf-8")) <= 1000
    assert result.metadata["truncated"] is True


async def test_small_output_is_not_marked_truncated():
    ex = executor()
    result = await ex.run(["echo", "small"], cwd="/tmp")
    assert result.metadata["truncated"] is False


# ── exit status / working directory ──────────────────────────────────────


async def test_nonzero_exit_status_is_reported_not_raised(tmp_path):
    ex = executor(allowed_executables=["sh"])
    result = await ex.run(["sh", "-c", "exit 7"], cwd=str(tmp_path))
    assert result.metadata["exit_code"] == 7


async def test_working_directory_is_the_one_requested(tmp_path):
    ex = executor(allowed_executables=["pwd"])
    result = await ex.run(["pwd"], cwd=str(tmp_path))
    assert result.content.strip() == str(tmp_path)


async def test_missing_executable_is_a_deterministic_failure():
    ex = executor(allowed_executables=["/nonexistent/definitely/not/here"])
    with pytest.raises(ExecutionError) as excinfo:
        await ex.run(["/nonexistent/definitely/not/here"], cwd="/tmp")
    assert excinfo.value.code == ExecutionErrorCode.INVALID_ARGUMENTS


# ── resource limits (best-effort, POSIX) ─────────────────────────────────


async def test_memory_rlimit_is_applied_to_the_child():
    """A process that tries to allocate well past the configured RLIMIT_AS
    must fail rather than being allowed to exhaust host memory."""

    ex = executor(allowed_executables=["python3"])
    result = await ex.run(
        ["python3", "-c", "b = bytearray(2 * 1024 * 1024 * 1024)"],  # 2GiB > 512MiB rlimit
        cwd="/tmp",
    )
    assert result.metadata["exit_code"] != 0
