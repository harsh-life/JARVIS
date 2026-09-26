"""Kernel-enforced confinement for `system.restricted` processes.

`server.fs` and `server.net` constrain what *this codebase's* adapters do. A
spawned process is not this codebase: it is whatever the allow-listed binary
does. Without confinement it runs as the server's own OS user, so it can read
every file that user can — other users' sandbox roots, the database, the
configuration — read `/proc/<server-pid>/environ` (where an `env:`-sourced KEK
and the superuser token live), and open network connections that never pass
through `server.net`. That is 09 §8's "a tool that ignores the helper" and
10 §3's "any network mechanism", reached through an *authorized* call rather
than a compromise. Hence this module.

Two unprivileged Linux mechanisms, applied in the child between `fork` and
`exec`, after `no_new_privs`:

* **Landlock** (`landlock(7)`): filesystem access is denied everywhere except
  read/execute on the system directories programs need, and read/write inside
  the process's own working directory (the task's temp root). On kernels that
  support it, TCP bind/connect is denied and the process is *scoped*: it can
  neither signal nor connect an abstract Unix socket outside its own domain.
  Landlock also stops it ptracing anything outside the domain.
* **seccomp**: creating a socket at all (`socket`, `io_uring_setup`) fails with
  `EPERM`, and so does any syscall made through a non-native ABI. Landlock does
  not cover UDP or pathname Unix sockets; this closes both.

The boundary is the kernel's, so it holds whatever the binary tries — the
allow-list is policy (which programs), confinement is the boundary (what any of
them can reach). Not a guarantee against a kernel exploit, and nothing here
touches memory or CPU: those remain `process.py`'s rlimits.
"""

from __future__ import annotations

import ctypes
import os
import platform
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable

LANDLOCK = "landlock"

# What a program needs to start and run: the dynamic loader and libraries, the
# binaries themselves, and the handful of /etc files libc reads by name. Deny
# by default means anything absent here is unreadable — /etc as a whole, /home,
# /root, /proc, /sys, /var, /run, and the server's own data directories.
DEFAULT_READ_ONLY_PATHS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
    "/etc/localtime",
    "/etc/alternatives",
    "/etc/nsswitch.conf",
    "/etc/passwd",
    "/etc/group",
)
_READ_ONLY_DEVICES = ("/dev/zero", "/dev/urandom", "/dev/random")
_READ_WRITE_DEVICES = ("/dev/null",)

# landlock(7) syscall numbers are the same on every architecture; the rest are not.
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_ARCHES = {
    # machine: (AUDIT_ARCH_*, socket, io_uring_setup, has_x32)
    "x86_64": (0xC000003E, 41, 425, True),
    "aarch64": (0xC00000B7, 198, 425, False),
}

_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1

_FS_EXECUTE = 1 << 0
_FS_WRITE_FILE = 1 << 1
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
_FS_REMOVE_DIR = 1 << 4
_FS_REMOVE_FILE = 1 << 5
_FS_MAKE_DIR = 1 << 7
_FS_MAKE_REG = 1 << 8
_FS_MAKE_SYM = 1 << 12
_FS_REFER = 1 << 13  # ABI 2
_FS_TRUNCATE = 1 << 14  # ABI 3
_FS_IOCTL_DEV = 1 << 15  # ABI 5
_NET_BIND_TCP = 1 << 0  # ABI 4
_NET_CONNECT_TCP = 1 << 1  # ABI 4
_SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0  # ABI 6
_SCOPE_SIGNAL = 1 << 1  # ABI 6

_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_ERRNO_EPERM = 0x00050000 | 1
_X32_SYSCALL_BIT = 0x40000000

_BPF_LD_W_ABS = 0x20
_BPF_JEQ_K = 0x15
_BPF_JGE_K = 0x35
_BPF_RET_K = 0x06


class ConfinementUnavailable(Exception):
    """This host cannot confine a process the way `landlock` mode requires."""


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_uint16),
        ("jt", ctypes.c_uint8),
        ("jf", ctypes.c_uint8),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


@lru_cache(maxsize=1)
def landlock_abi() -> int:
    """The kernel's Landlock ABI version, or 0 where there is none."""

    if not sys.platform.startswith("linux") or platform.machine() not in _ARCHES:
        return 0
    try:
        version = _libc().syscall(
            _SYS_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0),
            ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION),
        )
    except (OSError, AttributeError):
        return 0
    return max(0, int(version))


def available() -> bool:
    return landlock_abi() >= 1


def _handled_fs(abi: int) -> int:
    mask = (1 << 13) - 1  # ABI 1: EXECUTE .. MAKE_SYM
    if abi >= 2:
        mask |= _FS_REFER
    if abi >= 3:
        mask |= _FS_TRUNCATE
    if abi >= 5:
        mask |= _FS_IOCTL_DEV
    return mask


def _seccomp_program(machine: str) -> ctypes.Array:
    arch, socket_nr, io_uring_nr, has_x32 = _ARCHES[machine]
    # Built as a list with a symbolic DENY target, then jump offsets resolved:
    # a BPF jump is relative to the instruction after it.
    body: list[tuple[int, int | str, int | str, int]] = [
        (_BPF_LD_W_ABS, 0, 0, 4),  # A = seccomp_data.arch
        (_BPF_JEQ_K, 0, "deny", arch),  # a non-native ABI never gets through
        (_BPF_LD_W_ABS, 0, 0, 0),  # A = seccomp_data.nr
    ]
    if has_x32:
        body.append((_BPF_JGE_K, "deny", 0, _X32_SYSCALL_BIT))
    body += [
        (_BPF_JEQ_K, "deny", 0, socket_nr),
        (_BPF_JEQ_K, "deny", 0, io_uring_nr),
        (_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW),
    ]
    deny_index = len(body)
    body.append((_BPF_RET_K, 0, 0, _SECCOMP_RET_ERRNO_EPERM))

    def offset(index: int, target: int | str) -> int:
        return deny_index - (index + 1) if target == "deny" else int(target)

    program = (_SockFilter * len(body))()
    for index, (code, jt, jf, k) in enumerate(body):
        program[index] = _SockFilter(code, offset(index, jt), offset(index, jf), k)
    return program


@dataclass
class ConfinementPlan:
    """A ruleset built in the parent, applied in the child.

    Everything that allocates or opens paths happens in the parent; the child
    only makes four syscalls, so the work between `fork` and `exec` is minimal.
    """

    ruleset_fd: int
    _program: ctypes.Array = field(repr=False)
    _fprog: _SockFprog = field(repr=False)

    def apply_in_child(self) -> None:
        """Runs in the forked child, before `exec`. Any failure raises, which
        makes `subprocess` abort the exec: an unconfined child never runs."""

        libc = _libc()
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "no_new_privs")
        if libc.syscall(_SYS_LANDLOCK_RESTRICT_SELF, self.ruleset_fd, ctypes.c_uint32(0)) != 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self")
        os.close(self.ruleset_fd)
        if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(self._fprog), 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "seccomp")

    def close(self) -> None:
        try:
            os.close(self.ruleset_fd)
        except OSError:
            pass


def build_plan(*, writable: str, read_only: Iterable[str] = ()) -> ConfinementPlan:
    """Build the ruleset for one process: read/execute on `DEFAULT_READ_ONLY_PATHS`
    plus `read_only`, read/write (never execute) inside `writable`."""

    abi = landlock_abi()
    if abi < 1:
        raise ConfinementUnavailable("this kernel does not provide Landlock")

    libc = _libc()
    handled_fs = _handled_fs(abi)
    attr = _RulesetAttr(
        handled_access_fs=handled_fs,
        handled_access_net=(_NET_BIND_TCP | _NET_CONNECT_TCP) if abi >= 4 else 0,
        scoped=(_SCOPE_ABSTRACT_UNIX_SOCKET | _SCOPE_SIGNAL) if abi >= 6 else 0,
    )
    ruleset_fd = libc.syscall(
        _SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)),
        ctypes.c_uint32(0),
    )
    if ruleset_fd < 0:
        raise ConfinementUnavailable(f"landlock_create_ruleset failed (errno {ctypes.get_errno()})")

    file_read = _FS_EXECUTE | _FS_READ_FILE
    dir_read = file_read | _FS_READ_DIR
    file_rw = _FS_READ_FILE | _FS_WRITE_FILE | _FS_TRUNCATE
    workdir = (
        _FS_READ_FILE | _FS_READ_DIR | _FS_WRITE_FILE | _FS_REMOVE_DIR | _FS_REMOVE_FILE
        | _FS_MAKE_DIR | _FS_MAKE_REG | _FS_MAKE_SYM | _FS_REFER | _FS_TRUNCATE
    )

    def add(path: str, *, file_rights: int, dir_rights: int, required: bool = False) -> None:
        try:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        except OSError:
            if required:
                raise ConfinementUnavailable(f"cannot open {path!r} for the ruleset") from None
            return  # an absent system path needs no rule
        try:
            is_dir = os.path.isdir(f"/proc/self/fd/{fd}") or os.path.isdir(path)
            rights = (dir_rights if is_dir else file_rights) & handled_fs
            rule = _PathBeneathAttr(allowed_access=rights, parent_fd=fd)
            if libc.syscall(
                _SYS_LANDLOCK_ADD_RULE, ruleset_fd, _LANDLOCK_RULE_PATH_BENEATH,
                ctypes.byref(rule), ctypes.c_uint32(0),
            ) != 0:
                raise ConfinementUnavailable(
                    f"landlock_add_rule failed for {path!r} (errno {ctypes.get_errno()})"
                )
        finally:
            os.close(fd)

    try:
        for path in (*DEFAULT_READ_ONLY_PATHS, *read_only):
            add(path, file_rights=file_read, dir_rights=dir_read)
        for path in _READ_ONLY_DEVICES:
            add(path, file_rights=_FS_READ_FILE, dir_rights=0)
        for path in _READ_WRITE_DEVICES:
            add(path, file_rights=file_rw, dir_rights=0)
        add(writable, file_rights=0, dir_rights=workdir, required=True)
    except BaseException:
        os.close(ruleset_fd)
        raise

    program = _seccomp_program(platform.machine())
    fprog = _SockFprog(len(program), ctypes.cast(program, ctypes.POINTER(_SockFilter)))
    return ConfinementPlan(ruleset_fd=ruleset_fd, _program=program, _fprog=fprog)


__all__ = [
    "DEFAULT_READ_ONLY_PATHS",
    "LANDLOCK",
    "ConfinementPlan",
    "ConfinementUnavailable",
    "available",
    "build_plan",
    "landlock_abi",
]
