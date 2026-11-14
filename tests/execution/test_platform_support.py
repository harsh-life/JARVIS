"""What the execution layer does on a host that lacks the POSIX facilities it is
built on (cross-platform portability; Linux is the server host).

Windows is a development, test and client environment for JARVIS, not a
server host. Its Python has no `resource` module, no `preexec_fn`, no process
groups, no Unix-domain sockets, and no descriptor-relative (`dir_fd`,
`O_NOFOLLOW`, `O_DIRECTORY`) file syscalls. The modules that depend on them
must still be importable (so the rest of the server can be developed and tested
there) — and each protected operation must **refuse**, never run with weaker
guarantees:

* constrained process execution → `PLATFORM_UNSUPPORTED`, before any break-glass
  invocation is spent and before anything is spawned;
* the egress proxy and `unix:` internal listeners → `UnixSocketsUnavailable`,
  with nothing listening and no fallback to TCP;
* the filesystem sandbox → `PLATFORM_UNSUPPORTED` before its first syscall:
  nothing is created (not even its base directory), read, written or deleted;
* the container engine → `ContainerError` rather than an invented uid/gid.

Every test here runs on every host: each refusal is exercised by simulating the
missing facility, and — where the host really lacks it (Windows) — natively.
"""

from __future__ import annotations

import io
import os
import socket
import sys
import tarfile
import uuid
import zipfile
from pathlib import Path

import pytest

from server.execution import containers, process
from server.execution.containers import ContainerEngine, ContainerError, EngineSettings
from server.execution.process import ConstrainedProcessExecutor
from server.fs import FilesystemSandbox
from server.fs import paths
from server.net import listen
from server.net.egress_proxy import EgressProxy, ProxyPolicy
from server.net.listen import UnixSocketsUnavailable, internal_listen_socket
from shared.schemas.execution import ExecutionError, ExecutionErrorCode
from tests.support import (
    DESCRIPTOR_CONTAINMENT_MISSING,
    POSIX_PROCESS_MISSING,
    UNIX_LISTENER_MISSING,
    DisposableHostExecutor,
)

SIMULATED = "simulated"
NATIVE = "native"


def _host(mode: str, missing_on_this_host: str | None):
    """`simulated` runs everywhere; `native` only where the facility is absent."""

    if mode == NATIVE and missing_on_this_host is None:
        pytest.skip("this host provides the facility; the simulated case covers the refusal")


# ── constrained process execution ───────────────────────────────────────────


@pytest.fixture(params=[SIMULATED, NATIVE])
def no_process_controls(request, monkeypatch):
    _host(request.param, POSIX_PROCESS_MISSING)
    if request.param == SIMULATED:
        monkeypatch.setattr(process, "posix_process_controls_missing", lambda: "rlimits (simulated)")


async def test_a_confined_run_is_refused_without_posix_process_controls(no_process_controls, tmp_path):
    executor = ConstrainedProcessExecutor(allowed_executables=[sys.executable])
    with pytest.raises(ExecutionError) as excinfo:
        await executor.run([sys.executable, "-c", "print('ran')"], cwd=str(tmp_path))
    assert excinfo.value.code is ExecutionErrorCode.PLATFORM_UNSUPPORTED
    assert "nothing was run" in str(excinfo.value)


async def test_break_glass_is_refused_too_and_no_invocation_is_spent(no_process_controls, tmp_path):
    marker = tmp_path / "ran"
    executor = DisposableHostExecutor(allowed_executables=[sys.executable])
    with pytest.raises(ExecutionError) as excinfo:
        await executor.run([sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"], cwd=str(tmp_path))
    assert excinfo.value.code is ExecutionErrorCode.PLATFORM_UNSUPPORTED
    assert executor.break_glass.claims == []       # refused before the record was consulted
    assert executor.break_glass.invocations == []
    assert not marker.exists()


def test_the_process_module_imports_and_reports_what_is_missing():
    # Importable everywhere; on Windows it names exactly what is absent.
    reported = process.posix_process_controls_missing()
    if os.name == "posix":
        assert reported is None
    else:
        assert reported is not None and "rlimits" in reported


# ── Unix-domain listeners and the egress proxy ──────────────────────────────


@pytest.fixture(params=[SIMULATED, NATIVE])
def no_unix_sockets(request, monkeypatch):
    _host(request.param, UNIX_LISTENER_MISSING)
    if request.param == SIMULATED:
        monkeypatch.setattr(listen, "unix_listener_missing", lambda: "socket.AF_UNIX (simulated)")


def test_a_unix_listener_is_refused_and_creates_nothing(no_unix_sockets, tmp_path):
    path = tmp_path / "model.sock"
    with pytest.raises(UnixSocketsUnavailable, match="nothing is listening"):
        internal_listen_socket(f"unix:{path}")
    assert not path.exists()


def test_a_loopback_tcp_listener_is_unaffected(no_unix_sockets):
    sock = internal_listen_socket("127.0.0.1:0")
    try:
        assert sock.family == socket.AF_INET
    finally:
        sock.close()


async def test_the_egress_proxy_refuses_to_start_and_never_falls_back_to_tcp(no_unix_sockets, tmp_path):
    path = tmp_path / "egress.sock"
    proxy = EgressProxy(binding=f"unix:{path}", policy=ProxyPolicy(hosts=frozenset({"advisories.example.org"})))
    with pytest.raises(UnixSocketsUnavailable):
        await proxy.start()
    assert proxy._server is None   # nothing is serving, on any transport
    assert not path.exists()


def test_the_egress_proxy_still_accepts_only_a_unix_binding():
    # Unchanged: there is no TCP configuration to fall back to.
    with pytest.raises(ValueError, match="Unix socket only"):
        EgressProxy(binding="127.0.0.1:8080", policy=ProxyPolicy(hosts=frozenset({"advisories.example.org"})))


# ── the filesystem sandbox ──────────────────────────────────────────────────


@pytest.fixture(params=[SIMULATED, NATIVE])
def no_descriptor_containment(request, monkeypatch):
    _host(request.param, DESCRIPTOR_CONTAINMENT_MISSING)
    if request.param == SIMULATED:
        monkeypatch.setattr(paths, "descriptor_containment_missing", lambda: "os.O_DIRECTORY (simulated)")


def _unsupported(excinfo) -> None:
    assert excinfo.value.code is ExecutionErrorCode.PLATFORM_UNSUPPORTED
    assert "nothing was read, written, created or deleted" in str(excinfo.value)


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else b"<dir>") for p in sorted(root.rglob("*"))}


def _archives(root: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("inner.txt", "zip")
    (root / "a.zip").write_bytes(buffer.getvalue())
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        data = b"tar"
        info = tarfile.TarInfo("inner.txt")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    (root / "a.tar").write_bytes(buffer.getvalue())


def test_the_sandbox_is_inert_and_creates_not_even_its_base_root(no_descriptor_containment, tmp_path):
    base = tmp_path / "sandboxes"
    sandbox = FilesystemSandbox(base_root=str(base))
    assert not base.exists()
    for allocate in (
        lambda: sandbox.root_for(user_id=uuid.uuid4(), graph_id=None, label="notes"),
        lambda: sandbox.root_for(user_id=uuid.uuid4(), graph_id=uuid.uuid4(), label="shared"),
        lambda: sandbox.task_temp(task_id=uuid.uuid4()),
    ):
        with pytest.raises(ExecutionError) as excinfo:
            allocate()
        _unsupported(excinfo)
    assert not base.exists()
    sandbox.cleanup_task(task_id=uuid.uuid4())   # a task end still completes: there is nothing to remove


def test_every_sandbox_operation_refuses_before_touching_the_filesystem(no_descriptor_containment, tmp_path):
    # A directory that already exists, with content, standing in for a root a
    # caller might pass. Nothing may be read from, written to, or removed from it.
    root = tmp_path / "existing-root"
    (root / "dir").mkdir(parents=True)
    (root / "file.txt").write_text("original", encoding="utf-8", newline="\n")
    _archives(root)
    before = _tree(root)
    sandbox = FilesystemSandbox(base_root=str(tmp_path / "sandboxes"))
    operations = {
        "read_file": lambda: sandbox.read_file(str(root), "file.txt"),
        "stat": lambda: sandbox.stat(str(root), "file.txt"),
        "list_directory": lambda: sandbox.list_directory(str(root)),
        "list_subdirectory": lambda: sandbox.list_directory(str(root), "dir"),
        "write_file": lambda: sandbox.write_file(str(root), "file.txt", "overwritten"),
        "create_file": lambda: sandbox.create_file(str(root), "new.txt", "created"),
        "delete_file": lambda: sandbox.delete_file(str(root), "file.txt"),
        "bulk_delete": lambda: sandbox.bulk_delete(str(root), "dir"),
        "extract_zip": lambda: sandbox.extract_zip(str(root), "a.zip", "out"),
        "extract_tar": lambda: sandbox.extract_tar(str(root), "a.tar", "out"),
    }
    for name, operation in operations.items():
        with pytest.raises(ExecutionError) as excinfo:
            operation()
        assert excinfo.value.code is ExecutionErrorCode.PLATFORM_UNSUPPORTED, name
    assert _tree(root) == before


@pytest.mark.parametrize("payload", ["../../etc/passwd", "a/../../b", "..\\..\\windows\\system32", "/etc/passwd"])
def test_path_validation_still_rejects_traversal_first(no_descriptor_containment, tmp_path, payload):
    # Pure validation (no syscall) runs before the platform check, so a hostile
    # path is still named as one on every host.
    sandbox = FilesystemSandbox(base_root=str(tmp_path / "sandboxes"))
    with pytest.raises(ExecutionError) as excinfo:
        sandbox.read_file(str(tmp_path), payload)
    assert excinfo.value.code in (ExecutionErrorCode.FORBIDDEN_PATH, ExecutionErrorCode.SANDBOX_VIOLATION)


def test_the_sandbox_reports_what_is_missing_on_this_host():
    reported = paths.descriptor_containment_missing()
    if sys.platform.startswith(("linux", "darwin")):
        assert reported is None
    elif os.name == "nt":
        assert reported is not None and "dir_fd" in reported


# ── the container engine ────────────────────────────────────────────────────


def test_the_container_engine_refuses_a_host_without_posix_ids(monkeypatch):
    for name in ("geteuid", "getuid", "getgid"):
        monkeypatch.delattr(containers.os, name, raising=False)
    with pytest.raises(ContainerError, match="needs a POSIX host"):
        ContainerEngine(EngineSettings(podman="podman"))


def test_explicit_ids_still_construct_an_engine_anywhere():
    engine = ContainerEngine(EngineSettings(podman="podman"), euid=lambda: 1000, uid=1000, gid=1000)
    assert engine._uid == 1000 and engine._gid == 1000
