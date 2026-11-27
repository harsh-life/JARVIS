"""The process launcher: applies a child's restrictions, then `execve`s the target.

Why this exists. `process.py` used to apply resource limits, `no_new_privs`,
Landlock and seccomp from a Python `preexec_fn`. Python code running between
`fork` and `exec` in a multi-threaded server can deadlock the child before it
ever reaches `exec` (a lock held by another thread at fork time — the
allocator, logging, the import lock — is never released in the child). The
parent now spawns this file as a **fresh, single-threaded interpreter** with
no `preexec_fn` at all (`subprocess` forks and execs in C), and the
restrictions are applied here, where running Python is safe, before the
target replaces this process.

    server (many threads) ──fork+exec, no Python in between──▶ launcher (1 thread)
        launcher: read spec ─ setrlimit ─ no_new_privs ─ landlock ─ seccomp ─ execve(target)

The interface is deliberately small and closed:

* invoked only as `<server's own interpreter> -I -S -B <this file> --spec-fd N
  --status-fd M` by `process.py`, never by a user, never through a shell, and
  never setuid — it holds no privilege the server process does not have;
* the spec is one JSON object read from an inherited pipe, with a fixed set of
  keys, each validated (absolute target path, `argv[0] == target`, string-only
  argv/env, integer limits, a bounded BPF program); anything else is refused;
* **fail closed**: if a *mandatory* restriction cannot be applied, the target
  is never executed. The launcher reports the failure on the status pipe and
  exits 126. Optional (platform-dependent) limits are reported as such;
* the status pipe is close-on-exec: on a successful `execve` it closes with a
  final `ready` record that lists what was actually enforced; any later
  record is an error.

The launcher has no imports from the server — only the standard library — so
nothing the server loads (or that a task could have influenced) runs here.
"""

from __future__ import annotations

import ctypes
import json
import os
import resource
import sys

MAX_SPEC_BYTES = 4 * 1024 * 1024
MAX_FILTER_INSTRUCTIONS = 64

EXIT_REFUSED = 126
EXIT_EXEC_FAILED = 127

_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_SYS_LANDLOCK_RESTRICT_SELF = 446

RLIMITS = {
    "cpu": "RLIMIT_CPU",
    "as": "RLIMIT_AS",
    "nofile": "RLIMIT_NOFILE",
    "fsize": "RLIMIT_FSIZE",
    "nproc": "RLIMIT_NPROC",
}

SPEC_KEYS = frozenset({"target", "argv", "env", "rlimits", "mandatory", "ruleset_fd", "seccomp_filter"})


class Refused(Exception):
    """A spec or restriction failure; `code` is a short identifier."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


def validate_spec(spec: object) -> dict:
    """The spec, or `Refused`. Pure: also used by the parent's own tests."""

    if not isinstance(spec, dict) or set(spec) != SPEC_KEYS:
        raise Refused("invalid_spec", "unexpected keys")
    target, argv, env = spec["target"], spec["argv"], spec["env"]
    if not isinstance(target, str) or not target.startswith("/") or "\x00" in target:
        raise Refused("invalid_spec", "target must be an absolute path")
    if (not isinstance(argv, list) or not argv or not all(isinstance(a, str) and "\x00" not in a for a in argv)
            or argv[0] != target):
        raise Refused("invalid_spec", "argv must be strings starting with the target")
    if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and k and "=" not in k and "\x00" not in k + v
            for k, v in env.items()):
        raise Refused("invalid_spec", "env must map strings to strings")
    rlimits, mandatory = spec["rlimits"], spec["mandatory"]
    if not isinstance(rlimits, dict) or not set(rlimits) <= set(RLIMITS) or not all(
            type(v) is int and v >= 0 for v in rlimits.values()):
        raise Refused("invalid_spec", "rlimits")
    if not isinstance(mandatory, list) or not set(mandatory) <= set(rlimits):
        raise Refused("invalid_spec", "mandatory limits must be among the requested ones")
    ruleset_fd, program = spec["ruleset_fd"], spec["seccomp_filter"]
    if ruleset_fd is not None and (type(ruleset_fd) is not int or ruleset_fd < 3):
        raise Refused("invalid_spec", "ruleset_fd")
    if program is not None:
        if not isinstance(program, list) or not 0 < len(program) <= MAX_FILTER_INSTRUCTIONS:
            raise Refused("invalid_spec", "seccomp_filter length")
        for ins in program:
            if (not isinstance(ins, list) or len(ins) != 4 or not all(type(x) is int for x in ins)
                    or not (0 <= ins[0] < 1 << 16 and 0 <= ins[1] < 256 and 0 <= ins[2] < 256
                            and 0 <= ins[3] < 1 << 32)):
                raise Refused("invalid_spec", "seccomp_filter instruction")
    if (ruleset_fd is None) != (program is None):
        # Kernel confinement is both layers or neither (break-glass).
        raise Refused("invalid_spec", "landlock and seccomp go together")
    return spec


def apply_rlimits(rlimits: dict, mandatory: list) -> dict:
    """Set each limit (soft = hard). A requested value above the current hard
    limit is lowered to it — stricter, never looser. Returns each limit's
    status: `enforced` or `unavailable`. A mandatory limit that cannot be
    enforced raises `Refused`: the target never runs under a weaker sandbox."""

    status: dict[str, str] = {}
    for name, value in rlimits.items():
        limit = getattr(resource, RLIMITS[name], None)
        try:
            if limit is None:
                raise OSError("not provided by this platform")
            _, hard = resource.getrlimit(limit)
            effective = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(limit, (effective, effective))
            if resource.getrlimit(limit) != (effective, effective):
                raise OSError("the kernel did not keep the limit")
            status[name] = "enforced"
        except (ValueError, OSError) as exc:
            if name in mandatory:
                raise Refused("rlimit_failed", f"{name}: {exc}") from None
            status[name] = "unavailable"
    return status


def apply_kernel_confinement(ruleset_fd: int, program: list) -> None:
    """no_new_privs, then Landlock, then seccomp — each mandatory."""

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise Refused("no_new_privs_failed", str(ctypes.get_errno()))
    if libc.syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, ctypes.c_uint32(0)) != 0:
        raise Refused("landlock_failed", str(ctypes.get_errno()))
    os.close(ruleset_fd)
    filters = (_SockFilter * len(program))(*[_SockFilter(*ins) for ins in program])
    fprog = _SockFprog(len(program), ctypes.cast(filters, ctypes.POINTER(_SockFilter)))
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) != 0:
        raise Refused("seccomp_failed", str(ctypes.get_errno()))


def _report(status_fd: int, record: dict) -> None:
    data = (json.dumps(record, separators=(",", ":")) + "\n").encode()
    while data:
        data = data[os.write(status_fd, data):]


def _read_spec(spec_fd: int) -> object:
    chunks, total = [], 0
    while True:
        chunk = os.read(spec_fd, 65536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_SPEC_BYTES:
            raise Refused("invalid_spec", "spec too large")
        chunks.append(chunk)
    os.close(spec_fd)
    try:
        return json.loads(b"".join(chunks))
    except ValueError:
        raise Refused("invalid_spec", "not JSON") from None


def _fd_arg(argv: list[str], flag: str) -> int:
    if argv.count(flag) != 1:
        raise SystemExit(EXIT_REFUSED)
    value = argv[argv.index(flag) + 1]
    if not value.isdigit():
        raise SystemExit(EXIT_REFUSED)
    return int(value)


def main(argv: list[str]) -> None:
    if len(argv) != 4:
        raise SystemExit(EXIT_REFUSED)
    spec_fd, status_fd = _fd_arg(argv, "--spec-fd"), _fd_arg(argv, "--status-fd")
    # The status pipe must close when the target is exec'd: that EOF, after a
    # `ready` record, is how the parent knows exec succeeded.
    os.set_inheritable(status_fd, False)
    try:
        spec = validate_spec(_read_spec(spec_fd))
        limits = apply_rlimits(spec["rlimits"], spec["mandatory"])
        confined = spec["ruleset_fd"] is not None
        if confined:
            apply_kernel_confinement(spec["ruleset_fd"], spec["seccomp_filter"])
    except Refused as exc:
        _report(status_fd, {"error": exc.code, "detail": exc.detail[:200]})
        os._exit(EXIT_REFUSED)
    except Exception as exc:  # noqa: BLE001 — anything unexpected is a refusal, never a run
        _report(status_fd, {"error": "launcher_error", "detail": type(exc).__name__})
        os._exit(EXIT_REFUSED)
    _report(status_fd, {"ready": True, "limits": limits, "kernel_confinement": confined})
    try:
        os.execve(spec["target"], spec["argv"], spec["env"])
    except OSError as exc:
        _report(status_fd, {"error": "exec_failed", "errno": exc.errno or 0})
        os._exit(EXIT_EXEC_FAILED)


if __name__ == "__main__":
    main(sys.argv[1:])
