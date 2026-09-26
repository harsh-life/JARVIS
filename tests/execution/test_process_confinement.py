"""The `system.restricted` confinement boundary (`server/execution/confinement.py`).

`system.restricted` is reached through normal authorization — a user-approved,
step-up-fresh confirmation — not through a compromise. So what an allow-listed
program can reach is an *authorization* question, and before this boundary the
answer was "everything the server's OS user can": other users' sandbox roots,
the server's own process environment, and any network destination.

Each test here plays the program as an attacker and asserts the kernel refuses.
They need Landlock; on a host without it they skip, and
`test_landlock_mode_fails_closed_where_unavailable` asserts the executor then
refuses to run anything at all rather than run it unconfined.
"""

from __future__ import annotations

import os
import socket
import stat
import textwrap

import pytest

from server.execution import confinement
from server.execution.process import ConstrainedProcessExecutor
from shared.schemas.execution import ExecutionError, ExecutionErrorCode
from tests.support import DisposableHostExecutor

pytestmark = pytest.mark.asyncio

needs_landlock = pytest.mark.skipif(
    not confinement.available(), reason="kernel has no Landlock; landlock mode fails closed here"
)


def confined(**overrides) -> ConstrainedProcessExecutor:
    defaults = dict(
        allowed_executables=["sh", "cat", "python3", "kill"],
        default_timeout_seconds=10.0,
        max_timeout_seconds=15.0,
    )
    defaults.update(overrides)
    return ConstrainedProcessExecutor(**defaults)


async def _python(ex, code: str, cwd) -> tuple[int, str]:
    result = await ex.run(["python3", "-c", textwrap.dedent(code)], cwd=str(cwd))
    return result.metadata["exit_code"], result.content + result.metadata["stderr"]


# ── filesystem ────────────────────────────────────────────────────────────


@needs_landlock
async def test_another_users_sandbox_file_is_unreadable(tmp_path):
    other = tmp_path / "sandboxes" / "users" / "user-b" / "private" / "notes"
    other.mkdir(parents=True)
    (other / "diary.txt").write_text("user B's private text")
    workdir = tmp_path / "sandboxes" / "tasks" / "task-a" / "tmp"
    workdir.mkdir(parents=True)

    result = await confined().run(["cat", str(other / "diary.txt")], cwd=str(workdir))
    assert result.metadata["exit_code"] != 0
    assert "user B's private text" not in result.content


@needs_landlock
async def test_the_servers_own_environment_is_unreadable(tmp_path):
    """/proc/<server>/environ is where an `env:`-sourced KEK and the superuser
    token live. Same UID, so only the kernel boundary stops this read."""

    code, out = await _python(
        confined(), f"print(open('/proc/{os.getpid()}/environ','rb').read()[:20])", tmp_path
    )
    assert code != 0
    assert "PermissionError" in out


@needs_landlock
async def test_host_configuration_outside_the_allow_list_is_unreadable(tmp_path):
    result = await confined().run(["cat", "/etc/hostname"], cwd=str(tmp_path))
    assert result.metadata["exit_code"] != 0


@needs_landlock
async def test_writes_outside_the_working_directory_are_refused(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    outside = tmp_path / "escaped.txt"
    result = await confined().run(["sh", "-c", f"echo x > {outside}"], cwd=str(workdir))
    assert result.metadata["exit_code"] != 0
    assert not outside.exists()


@needs_landlock
async def test_the_working_directory_itself_is_fully_usable(tmp_path):
    result = await confined().run(
        ["sh", "-c", "echo hello > a.txt && mkdir d && mv a.txt d/b.txt && cat d/b.txt"],
        cwd=str(tmp_path),
    )
    assert result.metadata["exit_code"] == 0, result.metadata["stderr"]
    assert result.content.strip() == "hello"


@needs_landlock
async def test_a_file_the_process_wrote_cannot_be_executed(tmp_path):
    """No EXECUTE right in the working directory: a program cannot drop a
    binary/script and run it to get past the allow-list."""

    script = tmp_path / "payload.sh"
    script.write_text("#!/bin/sh\necho ran\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    result = await confined().run(["sh", "-c", "./payload.sh"], cwd=str(tmp_path))
    assert "ran" not in result.content
    assert result.metadata["exit_code"] != 0


# ── network ───────────────────────────────────────────────────────────────


@needs_landlock
async def test_tcp_connections_are_refused(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        code, out = await _python(
            confined(),
            f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=2)",
            tmp_path,
        )
    finally:
        listener.close()
    assert code != 0
    assert "PermissionError" in out


@needs_landlock
async def test_udp_is_refused(tmp_path):
    """Landlock does not cover UDP; the seccomp filter's socket() denial does."""

    code, out = await _python(
        confined(),
        "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto(b'x', ('1.1.1.1', 53))",
        tmp_path,
    )
    assert code != 0
    assert "PermissionError" in out


@needs_landlock
async def test_pathname_unix_sockets_are_refused(tmp_path):
    server_path = tmp_path / "srv.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(server_path))
    listener.listen(1)
    try:
        code, out = await _python(
            confined(),
            f"import socket; s=socket.socket(socket.AF_UNIX); s.connect({str(server_path)!r})",
            tmp_path / "..",
        )
    finally:
        listener.close()
    assert code != 0
    assert "PermissionError" in out


# ── the server process itself ─────────────────────────────────────────────


@needs_landlock
@pytest.mark.skipif(confinement.landlock_abi() < 6, reason="signal scoping needs Landlock ABI 6")
async def test_the_server_process_cannot_be_signalled(tmp_path):
    code, out = await _python(confined(), f"import os; os.kill({os.getpid()}, 0)", tmp_path)
    assert code != 0
    assert "PermissionError" in out


@needs_landlock
async def test_file_size_is_bounded(tmp_path):
    """RLIMIT_FSIZE: RLIMIT_AS bounds memory, not disk."""

    result = await confined().run(
        ["sh", "-c", "head -c 80000000 /dev/zero > big.bin; echo done"], cwd=str(tmp_path)
    )
    size = (tmp_path / "big.bin").stat().st_size if (tmp_path / "big.bin").exists() else 0
    assert size <= 64 * 1024 * 1024


# ── fail closed ───────────────────────────────────────────────────────────


async def test_landlock_mode_fails_closed_where_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(confinement, "landlock_abi", lambda: 0)
    ex = confined(allowed_executables=["sh"])
    marker = tmp_path / "ran"
    with pytest.raises(ExecutionError) as excinfo:
        await ex.run(["sh", "-c", f"touch {marker}"], cwd=str(tmp_path))
    assert excinfo.value.code == ExecutionErrorCode.PLATFORM_UNSUPPORTED
    assert not marker.exists()


async def test_there_is_no_confinement_mode_to_choose():
    """20 §2.5: the global `unconfined` switch is gone — not refused, absent.
    The only way a child runs without the kernel layer is a break-glass
    record (`tests/execution/test_break_glass_executor.py`)."""

    assert not hasattr(confinement, "UNCONFINED") and not hasattr(confinement, "MODES")
    for mode in ("unconfined", "landlock"):
        with pytest.raises(TypeError):
            ConstrainedProcessExecutor(allowed_executables=["sh"], confinement_mode=mode)


# ── allow-list integrity (both paths) ─────────────────────────────────────


@pytest.mark.parametrize("mode", ["break_glass", confinement.LANDLOCK])
async def test_a_path_override_cannot_swap_the_allow_listed_binary(mode, tmp_path):
    """A bare allow-list name is exec'd by its resolved absolute path. Before,
    `exec` searched the child's PATH — which includes model-supplied overrides —
    so `PATH=/attacker/dir` ran any file named like the allow-listed program."""

    if mode == confinement.LANDLOCK and not confinement.available():
        pytest.skip("kernel has no Landlock")
    decoy_dir = tmp_path / "decoy"
    decoy_dir.mkdir()
    decoy = decoy_dir / "cat"
    decoy.write_text("#!/bin/sh\necho DECOY\n")
    decoy.chmod(0o755)
    (tmp_path / "f.txt").write_text("genuine")

    ex = (DisposableHostExecutor(allowed_executables=["cat"]) if mode == "break_glass"
          else ConstrainedProcessExecutor(allowed_executables=["cat"]))
    result = await ex.run(
        ["cat", "f.txt"], cwd=str(tmp_path), env_overrides={"PATH": str(decoy_dir)}
    )
    assert "DECOY" not in result.content
    assert result.content == "genuine"


@pytest.mark.parametrize("key", ["LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "DYLD_INSERT_LIBRARIES", "GCONV_PATH"])
async def test_loader_controlling_env_overrides_are_refused(key, tmp_path):
    # On the break-glass path: the loader protections are among what a record
    # does *not* remove (20 §2.1, OD-EXEC-3).
    ex = DisposableHostExecutor(allowed_executables=["cat"])
    with pytest.raises(ExecutionError) as excinfo:
        await ex.run(["cat", "/dev/null"], cwd=str(tmp_path), env_overrides={key: "./evil.so"})
    assert excinfo.value.code == ExecutionErrorCode.INVALID_ARGUMENTS
    assert ex.break_glass.claims == []  # refused before a record was consulted: nothing spent
