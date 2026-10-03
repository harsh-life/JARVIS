"""The external runtime's container — Phase 6 slice 6C (OD-AF-11/12/14).

An external agent runtime (OD-AF-6: Browser Use) runs in one disposable
container per run, launched by the JARVIS server's own unprivileged user
through a **rootless** Podman with **gVisor `runsc`** as the OCI runtime. The
container is execution infrastructure, never an authority: it receives the
run's sockets and a few values, and the decisions about what the run may do
were made before it started.

**The launch is fixed here, not configurable** (`run_argv`):

* `--runtime=runsc`, with gVisor's own `network=none` (loopback only, for
  the image's forwarders) and `host-uds=open` (a host socket is reachable
  only if it was mounted in), and Podman's `--network=none` — **no network
  interface** (OD-AF-12): the run's sockets are the only way out;
* non-root (`--userns=keep-id`, the server's own uid — never 0), a read-only
  root with no tmpfs (`--read-only --read-only-tmpfs=false`), all
  capabilities dropped, `no-new-privileges`;
* exactly two host mounts: the run's socket directory (`/run/jarvis`) and
  its scratch directory (`/scratch`, the only writable place) — and a
  read-only, empty `/tmp`, because gVisor would otherwise give the sandbox a
  writable in-memory one;
* only a digest-pinned image (`name@sha256:<64 hex>`), never pulled at run
  time (`--pull=never`; OD-AF-14 provisions it);
* only the run's own `JARVIS_*` values in its environment — and Podman
  itself is started with a from-scratch environment, so nothing of the
  server's (a provider key, the KEK source, a proxy, a session) can reach
  the container; `--http-proxy=false` as well;
* no log kept on the host (`--log-driver=none`): results come back through
  scratch, as data;
* labels on every container (`io.jarvis.managed`, `…run_id`, `…agent_id`)
  and a name derived from the run, so the kill path and reconciliation can
  find exactly the run's.

**The kill path** is deterministic: `podman stop --time=<grace>` (SIGTERM,
then SIGKILL after the grace) and `podman rm --force`, always both.
**Reconciliation** removes every managed container that is not a live run's
— by the engine's own container id, never by a name or label the container
could have chosen.

Podman is driven only by argv (`asyncio.create_subprocess_exec`, never a
shell). `ContainerEngine.verify` refuses to run as root, and refuses a
Podman that is not rootless or has no `runsc`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

CONTAINER_SOCKETS = "/run/jarvis"
CONTAINER_SCRATCH = "/scratch"
LABEL_MANAGED = "io.jarvis.managed"
LABEL_RUN = "io.jarvis.run_id"
LABEL_AGENT = "io.jarvis.agent_id"
NAME_PREFIX = "jarvis-run-"
RUNTIME = "runsc"

_IMAGE = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:\.[a-z0-9-]+)*(?::[0-9]{1,5})?"   # registry host[:port] or first segment
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"                                # path segments
    r"@sha256:[0-9a-f]{64}$"
)
_ENV_KEY = re.compile(r"^JARVIS_[A-Z0-9_]{1,40}$")
_NAME = re.compile(rf"^{NAME_PREFIX}[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$")
_ID = re.compile(r"^[0-9a-f]{12,64}$")
# Directories a run's mount may never be, or be inside of.
_FORBIDDEN_MOUNTS = ("/", "/home", "/root", "/etc", "/var/run", "/run", "/proc", "/sys", "/dev", "/usr", "/bin",
                     "/sbin", "/lib", "/boot")
_ENGINE_ENV = ("PATH", "HOME", "XDG_RUNTIME_DIR", "LANG")


class ContainerError(Exception):
    """The container could not be started, waited on or removed as asked."""


def container_name(run_id: uuid.UUID) -> str:
    return f"{NAME_PREFIX}{run_id}"


def _mount_dir(path: str) -> str:
    if not os.path.isabs(path):
        raise ValueError(f"a mount must be an absolute path: {path!r}")
    normal = os.path.normpath(path)
    if normal in _FORBIDDEN_MOUNTS or any(normal.endswith(s) for s in (".sock", ".socket")):
        raise ValueError(f"not a run's own directory: {path!r}")
    return normal


@dataclass(frozen=True)
class ContainerSpec:
    """One run's container. Everything here is decided by JARVIS before the
    run starts; nothing comes from the runtime."""

    run_id: uuid.UUID
    agent_id: uuid.UUID
    image: str
    socket_dir: str
    scratch_dir: str
    env: Mapping[str, str] = field(default_factory=dict)
    command: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _IMAGE.fullmatch(self.image):
            raise ValueError("only a digest-pinned image (name@sha256:<64 hex>) may run")
        object.__setattr__(self, "socket_dir", _mount_dir(self.socket_dir))
        object.__setattr__(self, "scratch_dir", _mount_dir(self.scratch_dir))
        if self.socket_dir == self.scratch_dir:
            raise ValueError("the socket and scratch directories are distinct")
        for key, value in self.env.items():
            if not _ENV_KEY.fullmatch(key):
                raise ValueError(f"not a run value: {key!r}")
            if any(c in value for c in "\x00\n\r"):
                raise ValueError(f"{key}: control characters")
        object.__setattr__(self, "env", dict(self.env))
        object.__setattr__(self, "command", tuple(str(c) for c in self.command))
        if any("\x00" in c for c in self.command):
            raise ValueError("command: NUL")


@dataclass(frozen=True)
class EngineSettings:
    podman: str
    ignore_cgroups: bool = False     # the operator's explicit choice where cgroups are not delegated
    memory_mb: int = 2048
    cpus: float = 2.0
    pids_limit: int = 512
    kill_grace_seconds: int = 10
    command_timeout_seconds: float = 120.0


def run_argv(spec: ContainerSpec, settings: EngineSettings, *, uid: int, gid: int) -> list[str]:
    """The exact `podman run` of one run's container."""

    if uid == 0 or gid == 0:
        raise ContainerError("the container never runs as root")
    flags = [
        f"--runtime={RUNTIME}",
        "--runtime-flag=network=none",
        "--runtime-flag=host-uds=open",
        *(["--runtime-flag=ignore-cgroups"] if settings.ignore_cgroups else []),
        "--network=none",
        "--read-only",
        "--read-only-tmpfs=false",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--userns=keep-id",
        f"--user={uid}:{gid}",
        "--pull=never",
        "--http-proxy=false",
        "--log-driver=none",
        f"--pids-limit={settings.pids_limit}",
        f"--memory={settings.memory_mb}m",
        f"--cpus={settings.cpus}",
        f"--name={container_name(spec.run_id)}",
        "--hostname=jarvis-run",
        f"--label={LABEL_MANAGED}=1",
        f"--label={LABEL_RUN}={spec.run_id}",
        f"--label={LABEL_AGENT}={spec.agent_id}",
        f"--mount=type=bind,src={spec.socket_dir},dst={CONTAINER_SOCKETS}",
        f"--mount=type=bind,src={spec.scratch_dir},dst={CONTAINER_SCRATCH}",
        # gVisor mounts a writable in-memory /tmp unless the spec names one:
        # name it, read-only, so the run's scratch is its only writable place.
        "--mount=type=tmpfs,dst=/tmp,ro=true,tmpfs-size=1k",
        *[f"--env={key}={value}" for key, value in sorted(spec.env.items())],
    ]
    return [settings.podman, "run", "--detach", *flags, spec.image, *spec.command]


# ── the per-run workspace ───────────────────────────────────────────────────


def prepare_workspace(base: Path, run_id: uuid.UUID) -> tuple[Path, Path]:
    """`<base>/<run_id>/{sockets,scratch}`, each `0700` and the server's own.
    A path that already exists (or a planted symlink) is refused."""

    base = Path(base)
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = base / str(run_id)
    try:
        os.mkdir(root, 0o700)
    except FileExistsError:
        raise ContainerError(f"a workspace for run {run_id} already exists") from None
    if root.is_symlink() or not root.resolve().is_relative_to(base.resolve()):
        raise ContainerError("the workspace escapes its base")
    sockets, scratch = root / "sockets", root / "scratch"
    for directory in (sockets, scratch):
        os.mkdir(directory, 0o700)
        os.chmod(directory, 0o700)
    os.chmod(root, 0o700)
    return sockets, scratch


def remove_workspace(base: Path, run_id: uuid.UUID) -> None:
    root = Path(base) / str(run_id)
    if root.is_symlink():
        root.unlink()
        return
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


# ── the engine ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ManagedContainer:
    id: str
    name: str
    run_id: uuid.UUID | None
    state: str


class ContainerEngine:
    def __init__(self, settings: EngineSettings, *, euid: Callable[[], int] = os.geteuid, uid: int | None = None,
                 gid: int | None = None, passthrough: Sequence[str] = ()) -> None:
        self.settings = settings
        self._euid = euid
        self._uid = os.getuid() if uid is None else uid
        self._gid = os.getgid() if gid is None else gid
        self._passthrough = tuple(passthrough)

    def _env(self) -> dict[str, str]:
        """From scratch: only what Podman itself needs to find its rootless
        state — never the server's own variables."""

        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
        for key in (*_ENGINE_ENV, *self._passthrough):
            if key in ("PATH", "LANG"):
                continue
            value = os.environ.get(key)
            if value is not None:
                env[key] = value
        return env

    async def _podman(self, *args: str, timeout: float | None = None) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            self.settings.podman, *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self._env())
        try:
            out, _err = await asyncio.wait_for(process.communicate(),
                                               timeout or self.settings.command_timeout_seconds)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise
        return process.returncode or 0, out.decode("utf-8", errors="replace")

    async def verify(self) -> None:
        """Refuse anything but a rootless Podman with `runsc`, run by a
        non-root user (OD-AF-11)."""

        if self._euid() == 0:
            raise ContainerError("the container engine is never driven as root (rootless only)")
        # `--runtime=runsc info` reports the runtime podman would really use.
        code, out = await self._podman(f"--runtime={RUNTIME}", "info", "--format=json")
        if code != 0:
            raise ContainerError("podman is unavailable")
        try:
            info = json.loads(out)
        except ValueError:
            raise ContainerError("podman info is unreadable") from None
        host = info.get("host") or {}
        if (host.get("security") or {}).get("rootless") is not True:
            raise ContainerError("podman is not rootless")
        if (host.get("ociRuntime") or {}).get("name") != RUNTIME:
            raise ContainerError("gVisor runsc is not available to podman")

    async def start(self, spec: ContainerSpec) -> str:
        argv = run_argv(spec, self.settings, uid=self._uid, gid=self._gid)
        name = container_name(spec.run_id)
        code, _out = await self._podman(*argv[1:])
        if code != 0:
            await self._remove(name)
            raise ContainerError("the run's container did not start")
        return name

    async def wait(self, name: str, *, timeout: float) -> int | None:
        """The container's exit code, or `None` if it is still running at
        `timeout` (the caller decides to stop it)."""

        self._check_name(name)
        try:
            code, out = await self._podman("wait", name, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        if code != 0:
            return None
        try:
            return int(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return None

    async def stop(self, name: str) -> None:
        """The kill path: stop (SIGTERM, then SIGKILL after the grace), then
        remove — both, always, whatever the first answered."""

        self._check_name(name)
        await self._stop_and_remove(name)

    async def _stop_and_remove(self, ref: str) -> None:
        grace = self.settings.kill_grace_seconds
        try:
            await self._podman("stop", f"--time={grace}", ref, timeout=grace + 30)
        except asyncio.TimeoutError:
            pass
        finally:
            await self._remove(ref)

    async def _remove(self, ref: str) -> None:
        try:
            await self._podman("rm", "--force", ref)
        except asyncio.TimeoutError:
            pass

    async def managed(self) -> list[ManagedContainer]:
        code, out = await self._podman("ps", "--all", f"--filter=label={LABEL_MANAGED}=1", "--format=json")
        if code != 0:
            raise ContainerError("podman ps failed")
        try:
            rows = json.loads(out or "[]") or []
        except ValueError:
            raise ContainerError("podman ps is unreadable") from None
        found = []
        for row in rows:
            cid = str(row.get("Id", ""))
            if not _ID.fullmatch(cid):
                continue
            names = row.get("Names") or []
            labels = row.get("Labels") or {}
            try:
                run_id = uuid.UUID(str(labels.get(LABEL_RUN, "")))
            except ValueError:
                run_id = None
            found.append(ManagedContainer(id=cid, name=str(names[0]) if names else cid,
                                          run_id=run_id, state=str(row.get("State", ""))))
        return found

    async def reconcile(self, live_runs: set[uuid.UUID]) -> list[str]:
        """Remove every managed container that is not a live run's; return
        their names. By container id only."""

        removed = []
        for container in await self.managed():
            if container.run_id is not None and container.run_id in live_runs \
                    and container.name == container_name(container.run_id):
                continue
            await self._stop_and_remove(container.id)
            removed.append(container.name)
        return removed

    @staticmethod
    def _check_name(name: str) -> None:
        if not _NAME.fullmatch(name):
            raise ContainerError("not a run's container")


__all__ = [
    "CONTAINER_SCRATCH",
    "CONTAINER_SOCKETS",
    "ContainerEngine",
    "ContainerError",
    "ContainerSpec",
    "EngineSettings",
    "ManagedContainer",
    "container_name",
    "prepare_workspace",
    "remove_workspace",
    "run_argv",
]
