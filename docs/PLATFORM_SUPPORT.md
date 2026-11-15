# Platform support

What each host operating system is supported for, and what the server does
on a host that lacks a facility a security control is built on. The rule
behind every row below: **an unsupported security-sensitive operation fails
closed** — it is refused before it does anything — and never falls back to a
weaker mechanism under the same name.

## Support boundaries

| Host | Supported for | Not claimed |
|---|---|---|
| **Linux** | **The canonical server host** — the platform the server's security controls are built for and validated on (`docs/DEPLOYMENT.md`). | — |
| macOS | Development and testing. Most of the suite runs; the protected operations below that need Linux refuse. | Server hosting. |
| **Windows** | **Development and client use only** — the server imports and its test suite runs, and the desktop companion and web client connect to a server hosted elsewhere. | **Server hosting.** Windows lacks the POSIX facilities the execution layer is built on, so its protected operations refuse rather than run. |

WSL2 is a Linux host for these purposes.

## Platform limitations of the protected operations

The Windows refusals are asserted by `tests/execution/test_platform_support.py`,
which simulates the missing facility on every host and, where the host really
lacks it (Windows), exercises it natively; the `F_GETPATH` refusal is asserted
by `tests/execution/test_fs_sandbox.py`.

| Capability | Linux | macOS | Windows |
|---|---|---|---|
| **Unix-domain sockets** (`unix:` internal listeners, the egress proxy) | Supported | Supported; `sun_path` is limited to 104 bytes, so a socket path in a deep temp directory can be too long | **Refused** (`UnixSocketsUnavailable`): Windows' CPython has neither `socket.AF_UNIX` nor `asyncio.start_unix_server`. Nothing listens and there is no fallback to TCP; the egress proxy accepts only a Unix binding. |
| **Constrained process execution** (`system.restricted`) | Supported: rlimits, own process group, Landlock + seccomp confinement (kernel ≥ 5.13) | **Confined runs refused** (`PLATFORM_UNSUPPORTED`): no Landlock. Only a claimed break-glass activation runs a child, unconfined (`docs/RUNNING_EXECUTION.md`, OD-BG-2), and macOS does not enforce `RLIMIT_AS` | **Refused** (`PLATFORM_UNSUPPORTED`): no `resource` (rlimits), no `preexec_fn`, no process groups. Refused before anything is spawned and before a break-glass invocation is spent. |
| **Filesystem sandbox** (`server/fs`) | Supported: `dir_fd` + `O_NOFOLLOW` walk, re-checked through `/proc/self/fd` where it is mounted | Supported: same walk, re-checked through `fcntl(F_GETPATH)`; if the kernel cannot answer, the operation is refused (`FORBIDDEN_PATH`) | **Refused** (`PLATFORM_UNSUPPORTED`): no descriptor-relative, no-follow syscalls (`os.supports_dir_fd` is empty) and permission bits are not an isolation boundary. Refused after pure path validation and before the first syscall; nothing is created, read, written or deleted. |
| **Rootless Podman** (`server/execution/containers.py`) | Supported only as rootless Podman with gVisor `runsc`, driven by a non-root user; anything else is refused (`ContainerError`) | Not validated. The engine's own check (`podman info` must report rootless with `runsc`) still applies and refuses anything else | **Refused** (`ContainerError`): the engine needs the process's POSIX uid/gid (`os.getuid`/`getgid`/`geteuid`) and never invents them. |

## What this does not change

- None of these refusals is configurable away: there is no switch that runs a
  protected operation with weaker guarantees on an unsupported host.
- The modules stay importable everywhere, so the rest of the server can be
  developed and tested on Windows and macOS — importability is not support
  for hosting.
- Validating Windows as a server host (an equivalent of the fs sandbox,
  process confinement and Unix-socket isolation) would be new work, not a
  configuration change.
