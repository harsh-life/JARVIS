"""Constrained process execution — the `system.restricted` capability's own
primitive (07 §2, 08 §6).

08 §6 / this branch's brief §4 are explicit that this is the highest-risk
surface in the whole execution layer and must stay "a separate, much-higher-
risk capability... never folded into ordinary capabilities." This module is
that isolation boundary's implementation:

* **never a shell** — `argv` is always a list, run via
  `asyncio.create_subprocess_exec` with no `shell` keyword set at all (never
  `asyncio.create_subprocess_shell`), so there is no shell to inject into in
  the first place, not merely a sanitized one;
* **executable allow-list, closed by default** — `ProcessExecutionConfig.
  allowed_executables` starts empty (server/config/schema.py); nothing runs
  until an operator explicitly opts an executable in;
* **no inherited host environment** — the child's environment is built from
  scratch (`_BASE_ENV` plus the tool's own declared overrides), never
  `os.environ.copy()`, so no ambient credential, API key, or `PYTHONPATH`
  reaches a spawned process by accident;
* **no Python between fork and exec** — the child is spawned as the
  launcher (`launcher.py`, a fresh single-threaded interpreter) with no
  `preexec_fn`; the launcher applies the limits and the kernel confinement and
  then `execve`s the target, failing closed if a mandatory restriction cannot
  be applied (a `preexec_fn` in this multi-threaded server could deadlock the
  child before exec);
* **bounded in every dimension that matters**: wall-clock timeout, `rlimit`
  CPU/address-space/file-descriptor/file-size ceilings (**mandatory**: the
  target never runs without them) plus `RLIMIT_NPROC` (optional — it is a
  per-UID count, see below), and a **combined** cap on retained
  stdout+stderr enforced by *not keeping past it* while still draining;
* **rlimits are per process, not per tree**: each descendant gets its own
  CPU/memory/file allowance, and `RLIMIT_NPROC` counts every process of the
  server's UID, not this tree. There is **no aggregate process-tree budget**
  here; the result says so (`aggregate_limits: not_enforced`). The bound on a
  tree is the wall clock plus the tree kill below;
* **whole-process-tree cleanup** — the child starts its own session. After a
  normal exit, a timeout, a cancellation or any error, every live process of
  the session is terminated (`SIGTERM`, then `SIGKILL`), not just the direct
  child — a descendant does not survive because its parent exited first. On
  Linux the leader stays unreaped until then, so the group/session ID cannot
  be reused by an unrelated process, and the tree is verified empty
  (`ProcessTree`). A descendant that calls `setsid()` escapes; only a cgroup
  would contain it;
* **kernel confinement, with no switch** — every child is confined by Landlock
  and seccomp (`confinement.py`), or does not run. The one exception is a
  break-glass record (20 §2): superuser-activated for one task, its owner and
  named executables, it removes that kernel layer — and only that — from a
  matching child. Every bullet above still applies to such a child.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import errno
import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from server.execution import confinement
from server.execution.break_glass import BreakGlassClaim, BreakGlassLookup, InvocationOutcome
from shared.schemas.execution import ExecutionError, ExecutionErrorCode, ExecutionResult

logger = logging.getLogger("hypermind.execution.process")

# A from-scratch environment — never the host's. PATH is the minimum needed
# for a plain executable lookup-free `argv[0]` (we always pass an absolute
# or allow-listed bare name, resolved by the allow-list check, not by PATH
# search semantics the child could be tricked by).
_BASE_ENV: Mapping[str, str] = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}

# Case-insensitive substrings that must never appear in a tool-declared env
# override key — defense in depth (server/execution never receives a raw
# secret to pass through in the first place, 12 §2/§4; this is the backstop
# if a future caller's declared overrides is built from the wrong source).
_BLOCKED_ENV_KEY_SUBSTRINGS = ("secret", "token", "password", "credential", "api_key", "apikey")

# Variables the dynamic loader (or glibc's iconv) acts on before `main` runs: an
# override pointing one at a file the process just wrote would run that code
# inside an allow-listed binary. Refused outright — no tool needs them.
_BLOCKED_ENV_KEY_PREFIXES = ("LD_", "DYLD_")
_BLOCKED_ENV_KEYS = frozenset({"GCONV_PATH"})

_MAX_ARGV_LENGTH = 256
_MAX_ARG_BYTES = 4096

# Conservative, fixed rlimit defaults (POSIX only). Not exposed as config in
# this pass — existence and enforcement is what §4 locks; exact tuning is an
# operator concern once real workloads exist.
_RLIMIT_AS_BYTES = 512 * 1024 * 1024
_RLIMIT_NOFILE = 64
# RLIMIT_NPROC is a per-*real-UID* ceiling — every process owned by that
# user, system-wide, not just this call's own descendants — and the
# calling process is (deliberately) not exempt from it the way a
# privileged/root one is. A tight value here is a live footgun, not just a
# defense-in-depth nicety: CI caught it directly (a non-root runner whose
# service account already had a few dozen unrelated processes made `sh`'s
# own `fork()` for a backgrounded `sleep &` fail outright, at NPROC=32 —
# invisible in a root-run dev environment, which Linux exempts from this
# limit entirely). Set high enough to act purely as a fork-bomb backstop,
# not as a ceiling any realistic shared-account process count could reach.
_RLIMIT_NPROC = 2048
# Largest single file the child may write (SIGXFSZ past it). Without it a
# process can fill the disk from its temp directory: RLIMIT_AS bounds memory,
# not storage (13 §2's "unlimited ... storage").
_RLIMIT_FSIZE_BYTES = 64 * 1024 * 1024

# Which limits must hold or the target does not run (the launcher fails
# closed). RLIMIT_NPROC is optional: it is a per-real-UID count, so it is a
# fork-bomb backstop for the whole account, never a per-execution guarantee,
# and some hosts refuse it. RLIMIT_AS is mandatory where the kernel enforces
# it (Linux); macOS accepts the call without enforcing an address-space limit.
MANDATORY_RLIMITS: tuple[str, ...] = ("cpu", "nofile", "fsize") + (
    ("as",) if sys.platform.startswith("linux") else ())

# The launcher: a fixed file in this package, run by this server's own
# interpreter — never configurable, never supplied by a caller.
LAUNCHER_PATH = Path(__file__).with_name("launcher.py")

_READ_CHUNK = 65536
_LAUNCH_GRACE_SECONDS = 5.0     # interpreter start-up, on top of the run's own wall clock
_DRAIN_GRACE_SECONDS = 0.5      # after the leader exits, for its output to finish
_STREAM_CLOSE_SECONDS = 2.0     # after the tree is ended, for the pipes to report EOF
_TERM_GRACE_SECONDS = 1.0       # SIGTERM → SIGKILL
_KILL_WAIT_SECONDS = 2.0        # after SIGKILL, before reporting survivors

_HAS_PROC = sys.platform.startswith("linux") and os.path.isdir("/proc/self")
# Observe the leader's exit without reaping it (Linux): see ProcessTree.
_CAN_WAIT_WITHOUT_REAPING = _HAS_PROC and hasattr(os, "waitid") and hasattr(os, "WNOWAIT")


def argv_digest(argv: Sequence[str]) -> str:
    """What the audit trail records about a break-glass command line: a
    digest, never the arguments themselves (the audit carries no payload)."""

    return hashlib.sha256(json.dumps(list(argv), separators=(",", ":")).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class ProcessOutcome:
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool               # stdout_truncated or stderr_truncated
    timed_out: bool
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    # Per limit: `enforced` / `unavailable`, as the launcher applied it.
    limits: Mapping[str, str] = field(default_factory=dict)
    # `verified` / `survivors` / `unverified` (ProcessTree.finish).
    cleanup: str = "unverified"
    kernel_confinement: bool = False


def _resolve_bare_names(entries: Sequence[str]) -> dict[str, str]:
    # A bare (no "/") allow-list entry is resolved *once*, at construction,
    # against the exact fixed PATH the child gets (_BASE_ENV["PATH"]) — never
    # against the host's ambient $PATH — to the one absolute path it names
    # today. That resolved path, plus the bare name itself (so a PATH-searched
    # invocation still works), are the only two spellings accepted for that
    # entry. Security review finding: matching on `os.path.basename(argv[0])`
    # alone (a bare-name entry accepting *any* absolute path sharing that
    # basename) let an operator who allow-listed "python3" unintentionally
    # authorize an unrelated binary at a different path with the same
    # basename — an `execve`-semantics bypass ("/"-containing argv[0] never
    # goes through PATH search, so nothing constrained which such path could
    # be supplied). Resolving to one concrete path closes that without losing
    # the ergonomics of writing a bare name in config.
    return {
        name: resolved
        for name in entries
        if "/" not in name
        for resolved in (shutil.which(name, path=_BASE_ENV["PATH"]),)
        if resolved
    }


@dataclass(frozen=True)
class _AllowList:
    """One allow-list: the entries as the operator wrote them, plus the one
    absolute path each bare entry resolved to."""

    entries: frozenset[str]
    resolved_by_name: Mapping[str, str]

    @classmethod
    def of(cls, entries: Sequence[str]) -> "_AllowList":
        return cls(frozenset(entries), _resolve_bare_names(entries))

    def entry_for(self, executable: str) -> str | None:
        """The configured entry `executable` is an accepted spelling of, or
        `None`. Exact match only — a bare entry additionally matches the one
        absolute path it resolved to at construction, never any other path
        that merely shares its basename. Every accepted spelling is one the
        operator either wrote down or that PATH resolution deterministically
        named."""

        if executable in self.entries:
            return executable
        for name, resolved in self.resolved_by_name.items():
            if resolved == executable:
                return name
        return None

    def exec_path(self, executable: str) -> str | None:
        """What to exec for an accepted spelling: always an absolute path,
        never a bare name. `exec` searches the *child's* PATH for a bare name,
        and the child environment includes model-supplied overrides —
        `PATH=/somewhere` would otherwise swap in any binary called "python3"
        for the allow-listed one."""

        if "/" in executable:
            return executable
        return self.resolved_by_name.get(executable)

    def absolute_paths(self) -> tuple[str, ...]:
        return tuple(
            path for path in (*self.entries, *self.resolved_by_name.values()) if path.startswith("/")
        )


class ConstrainedProcessExecutor:
    """One instance per process, configured from `ExecutionConfig.process`.

    Every child is confined by the kernel (`confinement.py`); where the host
    cannot confine, nothing runs (`PLATFORM_UNSUPPORTED`). There is no mode
    switch. The one exception is break-glass (20 §2): a child whose
    executable is on the *separate* break-glass list runs without the kernel
    layer only when `break_glass.claim` — a server-side record a superuser
    activated for this exact task, user and executable — says so, spending
    one of that record's invocations. Nothing in `argv`, the environment, or
    any other argument can ask for it.
    """

    def __init__(
        self,
        *,
        allowed_executables: Sequence[str] = (),
        default_timeout_seconds: float = 10.0,
        max_timeout_seconds: float = 60.0,
        max_output_bytes: int = 1_000_000,
        read_only_paths: Sequence[str] = (),
        break_glass_executables: Sequence[str] = (),
        break_glass: BreakGlassLookup | None = None,
    ) -> None:
        self._normal = _AllowList.of(allowed_executables)
        # 20 §2.3: its own list. Enabling break-glass never widens the
        # ordinary allow-list — an entry here runs only under a live record.
        self._break_glass_list = _AllowList.of(break_glass_executables if break_glass else ())
        self._break_glass = break_glass
        # Each allowed executable must itself be readable/executable inside the
        # confinement, wherever the operator installed it.
        self._read_only_paths = tuple(read_only_paths) + self._normal.absolute_paths()
        self._default_timeout = default_timeout_seconds
        self._max_timeout = max_timeout_seconds
        self._max_output_bytes = max_output_bytes

    def _assert_allowed(self, executable: str) -> str:
        if self._normal.entry_for(executable) is None:
            raise ExecutionError(
                ExecutionErrorCode.UNAUTHORIZED_EXECUTABLE,
                f"{executable!r} is not in the configured process allow-list",
            )
        path = self._normal.exec_path(executable)
        if path is None:
            raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, f"executable not found: {executable!r}")
        return path

    @staticmethod
    def _validate_argv(argv: Sequence[str]) -> list[str]:
        if not argv:
            raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, "argv must not be empty")
        if len(argv) > _MAX_ARGV_LENGTH:
            raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, "argv has too many elements")
        cleaned: list[str] = []
        for arg in argv:
            if not isinstance(arg, str):
                raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, "every argv element must be a string")
            if "\x00" in arg:
                raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, "argv element contains a NUL byte")
            if len(arg) > _MAX_ARG_BYTES:
                raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, "argv element too long")
            cleaned.append(arg)
        return cleaned

    def _build_argv(self, argv: Sequence[str]) -> list[str]:
        cleaned = self._validate_argv(argv)
        cleaned[0] = self._assert_allowed(cleaned[0])
        return cleaned

    def _claim_break_glass(
        self, executable: str, task_id: uuid.UUID | None, user_id: uuid.UUID | None
    ) -> tuple[BreakGlassClaim, str] | None:
        """A spent invocation and the path to exec, or `None` for the ordinary
        confined path. Asked last, after every other check on the call has
        passed, so a call refused for any other reason never spends one."""

        if self._break_glass is None or task_id is None or user_id is None:
            return None
        entry = self._break_glass_list.entry_for(executable)
        path = self._break_glass_list.exec_path(executable) if entry is not None else None
        if entry is None or path is None:
            return None
        claim = self._break_glass.claim(task_id=task_id, user_id=user_id, executable=entry)
        return None if claim is None else (claim, path)

    def _build_env(self, overrides: Mapping[str, str] | None) -> dict[str, str]:
        env = dict(_BASE_ENV)
        for key, value in (overrides or {}).items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, "env overrides must be strings")
            if key.startswith(_BLOCKED_ENV_KEY_PREFIXES) or key in _BLOCKED_ENV_KEYS:
                raise ExecutionError(
                    ExecutionErrorCode.INVALID_ARGUMENTS,
                    f"env override key {key!r} controls the dynamic loader and is refused",
                )
            lowered = key.lower()
            if any(blocked in lowered for blocked in _BLOCKED_ENV_KEY_SUBSTRINGS):
                raise ExecutionError(
                    ExecutionErrorCode.INVALID_ARGUMENTS,
                    f"env override key {key!r} looks credential-shaped and is refused",
                )
            if "\x00" in key or "\x00" in value:
                raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, "env override contains a NUL byte")
            env[key] = value
        return env

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env_overrides: Mapping[str, str] | None = None,
        timeout: float | None = None,
        task_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
    ) -> ExecutionResult:
        cleaned_argv = self._validate_argv(argv)
        child_env = self._build_env(env_overrides)
        requested = self._default_timeout if timeout is None else timeout
        if requested <= 0:
            # Not `timeout or self._default_timeout` — 0 is a legitimate,
            # explicit value in Python's truthiness sense but not in this
            # API's: `None` means "use the default", `0` means "the caller
            # asked for a zero timeout", and the latter is invalid, not a
            # request to fall back silently.
            raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, "timeout must be positive")
        effective_timeout = min(requested, self._max_timeout)

        plan: confinement.ConfinementPlan | None = None
        broken = self._claim_break_glass(cleaned_argv[0], task_id, user_id)
        if broken is not None:
            # 20 §2.1: a live record removes the kernel layer from this one
            # child — and nothing else. The environment was built from scratch
            # and checked above; the launcher, its mandatory rlimits, the
            # timeout, the output caps and the tree kill apply exactly as they
            # do when confined.
            claim, cleaned_argv[0] = broken
        else:
            claim = None
            cleaned_argv[0] = self._assert_allowed(cleaned_argv[0])
            # Fail closed where the kernel cannot confine: nothing runs, rather
            # than something running unconfined.
            try:
                plan = confinement.build_plan(writable=cwd, read_only=self._read_only_paths)
            except confinement.ConfinementUnavailable as exc:
                raise ExecutionError(
                    ExecutionErrorCode.PLATFORM_UNSUPPORTED,
                    f"process confinement is unavailable on this host ({exc}); nothing was run",
                ) from None

        if claim is None:
            outcome = await self._spawn(cleaned_argv, cwd, child_env, effective_timeout, plan)
        else:
            outcome = await self._spawn_under_break_glass(
                claim, argv, cleaned_argv, cwd, child_env, effective_timeout
            )
        stdout_cap, stderr_cap = self.output_limits()
        return ExecutionResult(
            content=outcome.stdout,
            units=1,
            metadata={
                "exit_code": outcome.exit_code,
                "stderr": outcome.stderr,
                "truncated": outcome.truncated,
                "stdout_truncated": outcome.stdout_truncated,
                "stderr_truncated": outcome.stderr_truncated,
                "timed_out": outcome.timed_out,
                "output_limit_bytes": self._max_output_bytes,
                "stdout_limit_bytes": stdout_cap,
                "stderr_limit_bytes": stderr_cap,
                "limits": dict(outcome.limits),
                # rlimits are per process; nothing here bounds a whole tree.
                "aggregate_limits": "not_enforced",
                "cleanup": outcome.cleanup,
                "kernel_confinement": outcome.kernel_confinement,
            },
        )

    async def _spawn_under_break_glass(
        self, claim: BreakGlassClaim, argv: Sequence[str], cleaned_argv: list[str], cwd: str,
        env: Mapping[str, str], timeout: float,
    ) -> ProcessOutcome:
        """The one unconfined path. The invocation is reported to the record
        store however it ends — exited, timed out, cancelled (the process group
        is killed first, as for any run), or never started."""

        assert self._break_glass is not None
        started = time.monotonic()
        result, exit_code = InvocationOutcome.NOT_STARTED, None
        try:
            outcome = await self._spawn(cleaned_argv, cwd, env, timeout, None)
            result = InvocationOutcome.TIMED_OUT if outcome.timed_out else InvocationOutcome.EXITED
            exit_code = outcome.exit_code
            return outcome
        except asyncio.CancelledError:
            result = InvocationOutcome.CANCELLED
            raise
        finally:
            try:
                self._break_glass.record_invocation(
                    claim, argv_sha256=argv_digest(argv), outcome=result, exit_code=exit_code,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            except Exception:  # pragma: no cover - the store's contract is not to raise
                logger.exception("break-glass invocation could not be recorded")

    # ── spawning: no Python between fork and exec ──────────────────────────

    def output_limits(self) -> tuple[int, int]:
        """The combined output budget, split deterministically: stderr may
        keep at most a quarter, stdout the rest. Retained output never exceeds
        `max_output_bytes` in total, however the two streams interleave."""

        stderr_cap = self._max_output_bytes // 4
        return self._max_output_bytes - stderr_cap, stderr_cap

    def _start(self, cleaned_argv: list[str], cwd: str, env: Mapping[str, str], timeout: float,
               plan: "confinement.ConfinementPlan | None") -> tuple[subprocess.Popen, int]:
        """Runs in a worker thread. `subprocess` forks and execs the launcher
        in C: no `preexec_fn`, so no Python ever runs in the forked child of
        this multi-threaded server. Returns the process and the read end of
        the launcher's status pipe."""

        rlimits = {
            "cpu": int(timeout) + 5,
            "as": _RLIMIT_AS_BYTES,
            "nofile": _RLIMIT_NOFILE,
            "fsize": _RLIMIT_FSIZE_BYTES,
            "nproc": _RLIMIT_NPROC,
        }
        spec = {
            "target": cleaned_argv[0], "argv": cleaned_argv, "env": dict(env),
            "rlimits": rlimits, "mandatory": list(MANDATORY_RLIMITS),
            "ruleset_fd": plan.ruleset_fd if plan is not None else None,
            "seccomp_filter": plan.seccomp_filter() if plan is not None else None,
        }
        payload = json.dumps(spec, separators=(",", ":")).encode()
        spec_r, spec_w = os.pipe()
        status_r, status_w = os.pipe()
        pass_fds = (spec_r, status_w, *((plan.ruleset_fd,) if plan is not None else ()))
        try:
            process = subprocess.Popen(
                [sys.executable, "-I", "-S", "-B", str(LAUNCHER_PATH),
                 "--spec-fd", str(spec_r), "--status-fd", str(status_w)],
                cwd=cwd, env={}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=pass_fds, close_fds=True, start_new_session=True,
            )
        except BaseException:
            for fd in (spec_r, spec_w, status_r, status_w):
                os.close(fd)
            raise
        os.close(spec_r)
        os.close(status_w)
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(spec_w, view):]
        except OSError:
            pass  # the launcher died before reading: its status (or silence) says so
        finally:
            os.close(spec_w)
        return process, status_r

    @staticmethod
    async def _read_status(status_r: int) -> list[dict]:
        """Read the launcher's status pipe to EOF, without tying up a worker
        thread. EOF comes when the target is exec'd (the pipe is
        close-on-exec) or the launcher exits."""

        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(status_r, "rb", buffering=0))
        data = b""
        try:
            while len(data) <= 65536:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                data += chunk
        finally:
            transport.close()
        records = []
        for line in data.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    @staticmethod
    def _check_launch(records: list[dict]) -> dict:
        """The launcher's `ready` record, or the refusal it reported. A target
        that was not confirmed as launched under its restrictions never ran."""

        errors = [r for r in records if "error" in r]
        if errors:
            error = errors[0]
            if error.get("error") == "exec_failed":
                if error.get("errno") in (errno.ENOENT, errno.ENOTDIR):
                    raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, "executable not found")
                raise ExecutionError(ExecutionErrorCode.INTERNAL,
                                     f"failed to start process (errno {error.get('errno')})")
            raise ExecutionError(
                ExecutionErrorCode.SANDBOX_VIOLATION,
                f"process restrictions could not be applied ({error.get('error')}); nothing was run",
            )
        ready = [r for r in records if r.get("ready") is True]
        if not ready:
            raise ExecutionError(ExecutionErrorCode.SANDBOX_VIOLATION,
                                 "the launcher did not confirm the process restrictions; nothing was run")
        return ready[0]

    async def _spawn(
        self, cleaned_argv: list[str], cwd: str, env: Mapping[str, str], timeout: float,
        plan: "confinement.ConfinementPlan | None",
    ) -> ProcessOutcome:
        loop = asyncio.get_running_loop()
        try:
            process, status_r = await asyncio.to_thread(self._start, cleaned_argv, cwd, env, timeout, plan)
        except FileNotFoundError as exc:
            raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, f"executable not found: {exc}") from exc
        except OSError as exc:
            raise ExecutionError(ExecutionErrorCode.INTERNAL, f"failed to start process: {exc}") from exc
        finally:
            if plan is not None:
                plan.close()

        # The wall clock is the child's: it starts when the child exists, not
        # while the spawn waited for a worker thread.
        deadline = loop.time() + timeout
        tree = ProcessTree(process)
        try:
            try:
                records = await asyncio.wait_for(self._read_status(status_r), timeout + _LAUNCH_GRACE_SECONDS)
            except asyncio.TimeoutError:
                raise ExecutionError(ExecutionErrorCode.SANDBOX_VIOLATION,
                                     "the launcher did not start in time; nothing was run") from None
            ready = self._check_launch(records)
            outcome = await self._communicate_bounded(tree, deadline)
            cleanup = await tree.finish()
            return dataclasses.replace(
                outcome, limits=dict(ready.get("limits") or {}), cleanup=cleanup,
                kernel_confinement=bool(ready.get("kernel_confinement")),
            )
        finally:
            # Normal completion, timeout, a refused launch, cancellation and any
            # exception all end here: every live process of the tree is
            # terminated and the leader reaped — even if this coroutine is
            # cancelled again while that happens.
            await tree.finish_shielded()

    async def _communicate_bounded(self, tree: "ProcessTree", deadline: float) -> ProcessOutcome:
        loop = asyncio.get_running_loop()
        stdout_cap, stderr_cap = self.output_limits()
        out, err = _CappedStream(stdout_cap), _CappedStream(stderr_cap)
        transports = []
        tasks: list[asyncio.Future] = []
        try:
            for stream, pipe in ((out, tree.process.stdout), (err, tree.process.stderr)):
                transports.append(await stream.attach(pipe))
            out_task, err_task = asyncio.ensure_future(out.drain()), asyncio.ensure_future(err.drain())
            exit_task = asyncio.ensure_future(tree.wait_exit())
            tasks = [out_task, err_task, exit_task]
            timed_out = False
            try:
                await asyncio.wait_for(asyncio.shield(exit_task), timeout=max(0.0, deadline - loop.time()))
            except asyncio.TimeoutError:
                # A timeout is a deterministic failure, not a hang: the whole
                # tree is ended before anything else is read.
                timed_out = True
                await tree.finish()
            else:
                # The leader is done, but a descendant may still hold the output
                # pipes open. Give the streams a short, bounded drain; then end
                # the tree, which closes them.
                _, pending = await asyncio.wait(
                    {out_task, err_task}, timeout=min(_DRAIN_GRACE_SECONDS, max(0.0, deadline - loop.time())))
                if pending:
                    await tree.finish()
            await asyncio.wait({out_task, err_task}, timeout=_STREAM_CLOSE_SECONDS)
            exit_code = -1 if timed_out else exit_task.result()
            return ProcessOutcome(
                exit_code=exit_code, stdout=out.text(), stderr=err.text(),
                truncated=out.truncated or err.truncated, timed_out=timed_out,
                stdout_truncated=out.truncated, stderr_truncated=err.truncated,
            )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for transport in transports:
                transport.close()


class _CappedStream:
    """One output pipe, read to EOF with bounded memory: at most `cap` bytes
    are kept; the rest is read and discarded so the child never blocks on a
    full pipe. What was kept survives cancellation of the reader."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.kept = bytearray()
        self.truncated = False
        self._reader: asyncio.StreamReader | None = None

    async def attach(self, pipe) -> asyncio.BaseTransport:
        loop = asyncio.get_running_loop()
        self._reader = asyncio.StreamReader(limit=2 * _READ_CHUNK)
        transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(self._reader), pipe)
        return transport

    async def drain(self) -> None:
        assert self._reader is not None
        while chunk := await self._reader.read(_READ_CHUNK):
            room = self.cap - len(self.kept)
            if room > 0:
                self.kept += chunk[:room]
            if len(chunk) > max(room, 0):
                self.truncated = True

    def text(self) -> str:
        return bytes(self.kept).decode("utf-8", errors="replace")


def _session_members(sid: int) -> list[int] | None:
    """Live (non-zombie) processes in session `sid`, from /proc — or `None`
    where there is no /proc to ask (the tree cannot be enumerated)."""

    if not _HAS_PROC:
        return None
    members = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        stat = _proc_stat(int(name))
        if stat is not None and stat[2] == sid and stat[0] != "Z":
            members.append(int(name))
    return members


def _proc_stat(pid: int) -> tuple[str, int, int] | None:
    """(state, pgrp, session) of one process, or `None` if it is gone."""

    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            fields = handle.read().rsplit(b")", 1)[1].split()
        return fields[0].decode(), int(fields[2]), int(fields[3])
    except (OSError, IndexError, ValueError):
        return None


def _signal_member(pid: int, sid: int, sig: int) -> None:
    """Signal one session member without a PID-reuse race: open a pidfd,
    re-check that it is still in `sid`, signal through the pidfd."""

    try:
        pidfd = os.pidfd_open(pid)
    except (ProcessLookupError, OSError):
        return
    try:
        stat = _proc_stat(pid)
        if stat is not None and stat[2] == sid:
            signal.pidfd_send_signal(pidfd, sig)
    except (ProcessLookupError, OSError):
        pass
    finally:
        os.close(pidfd)


class ProcessTree:
    """One launched child and every process it starts in its session.

    The child is started with `start_new_session=True`, so its PID is also its
    process-group and session ID. **On Linux the leader is not reaped until
    the whole tree has been dealt with**: its exit is observed with
    `waitid(WNOWAIT)`, which leaves it a zombie, and a zombie's PID — and so
    the group and session IDs — cannot be reused by an unrelated process.
    Signalling the group can therefore never hit a stranger, even after the
    direct child has exited. Live members are enumerated from /proc by
    session (catching a descendant that moved to another group with
    `setpgid`) and signalled through pidfds, then the leader is reaped.

    Elsewhere (macOS, where only break-glass runs can occur) there is no
    `waitid`/pidfd/procfs: the leader is reaped when it exits and the group is
    signalled `SIGTERM` → `SIGKILL` without being able to verify that it is
    empty; the cleanup status says `unverified`.

    Known limit, everywhere: a descendant that calls `setsid()` leaves the
    session and the group, and is not reached. Only a per-execution cgroup
    (not implemented here — docs/RUNNING_EXECUTION.md "Process trees") closes
    that.
    """

    def __init__(self, process: subprocess.Popen) -> None:
        self.process = process
        self.pid = process.pid
        self.exit_code: int | None = None
        self._reaped = False
        self._finish: asyncio.Task | None = None

    def poll_exit(self) -> int | None:
        if self.exit_code is not None:
            return self.exit_code
        try:
            if _CAN_WAIT_WITHOUT_REAPING:
                info = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if info is None:
                    return None
                code = info.si_status if info.si_code == os.CLD_EXITED else -info.si_status
            else:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
                if pid == 0:
                    return None
                code = os.waitstatus_to_exitcode(status)
                self._reaped = True
        except ChildProcessError:  # reaped by someone else: it has exited
            code, self._reaped = -1, True
        self.exit_code = code
        return code

    async def wait_exit(self) -> int:
        delay = 0.002
        while (code := self.poll_exit()) is None:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 0.05)
        return code

    def members(self) -> list[int] | None:
        return _session_members(self.pid)

    def _signal(self, sig: int) -> None:
        # The group: safe on Linux because the unreaped leader pins its ID.
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(self.pid, sig)
        # Members that left the group but not the session.
        for pid in self.members() or ():
            _signal_member(pid, self.pid, sig)

    async def _settled(self, seconds: float) -> bool:
        """True once the tree is verifiably empty within `seconds`."""

        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        while True:
            members = self.members()
            if members == []:
                return True
            if members is None and self.poll_exit() is not None:
                return False  # the leader is gone; the rest cannot be verified here
            if loop.time() >= end:
                return False
            await asyncio.sleep(0.01)

    async def _finish_now(self) -> str:
        if self.members() == []:
            status = "verified"
        else:
            self._signal(signal.SIGTERM)
            if not await self._settled(_TERM_GRACE_SECONDS):
                self._signal(signal.SIGKILL)
                await self._settled(_KILL_WAIT_SECONDS)
            members = self.members()
            status = "unverified" if members is None else ("verified" if not members else "survivors")
            if status == "survivors":
                logger.error("process tree %d still has %d live member(s) after SIGKILL", self.pid, len(members))
        await self._reap()
        return status

    async def _reap(self) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + _KILL_WAIT_SECONDS
        while self.poll_exit() is None:
            if loop.time() >= end:
                logger.error("process %d did not exit after SIGKILL; it is left to the OS", self.pid)
                return
            await asyncio.sleep(0.01)
        if not self._reaped:
            with contextlib.suppress(ChildProcessError):
                os.waitpid(self.pid, 0)
            self._reaped = True
        self.process.returncode = self.exit_code

    async def finish(self) -> str:
        """End every live process of the tree, then reap the leader. Safe to
        call more than once: the work is done once. Returns `verified` (the
        tree is empty), `survivors` (something outlived SIGKILL) or
        `unverified` (no way to enumerate the tree on this platform)."""

        if self._finish is None:
            self._finish = asyncio.ensure_future(self._finish_now())
        return await asyncio.shield(self._finish)

    async def finish_shielded(self) -> None:
        try:
            await self.finish()
        except asyncio.CancelledError:
            # Cancelled while cleaning up: finish anyway, then propagate.
            assert self._finish is not None
            await asyncio.gather(self._finish, return_exceptions=True)
            raise


__all__ = ["LAUNCHER_PATH", "MANDATORY_RLIMITS", "ConstrainedProcessExecutor", "ProcessOutcome", "ProcessTree",
           "argv_digest"]
