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
* **bounded in every dimension that matters**: wall-clock timeout, `rlimit`
  CPU/memory/file-descriptor/process-count ceilings (POSIX), and a hard cap
  on captured stdout/stderr enforced by *not reading past it* — a runaway
  process cannot be turned into a memory-exhaustion primitive by producing
  unbounded output;
* **whole-process-tree cleanup** — the child starts its own session
  (`start_new_session=True`); a timeout or cancellation signals the whole
  process group, not just the direct child, so a forked grandchild cannot
  outlive the call that spawned it;
* **kernel confinement, with no switch** — every child is confined by Landlock
  and seccomp (`confinement.py`), or does not run. The one exception is a
  break-glass record (20 §2): superuser-activated for one task, its owner and
  named executables, it removes that kernel layer — and only that — from a
  matching child. Every bullet above still applies to such a child.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import resource
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
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


def argv_digest(argv: Sequence[str]) -> str:
    """What the audit trail records about a break-glass command line: a
    digest, never the arguments themselves (the audit carries no payload)."""

    return hashlib.sha256(json.dumps(list(argv), separators=(",", ":")).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class ProcessOutcome:
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool


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

    @classmethod
    def _child_setup(cls, cpu_seconds: int, plan: "confinement.ConfinementPlan | None") -> None:
        # Runs in the child after fork, before exec. Confinement goes last so the
        # rlimit calls are not themselves subject to it; a failure applying it
        # raises, which aborts the exec — a child meant to be confined never
        # starts unconfined. `plan` is None only under a claimed break-glass record.
        cls._set_child_rlimits(cpu_seconds)
        if plan is not None:
            plan.apply_in_child()

    @staticmethod
    def _set_child_rlimits(cpu_seconds: int) -> None:
        # Runs in the child, after fork and before exec (POSIX preexec_fn).
        # Best-effort: a platform without one of these limits (e.g. a
        # sandboxed CI container that refuses RLIMIT_NPROC) degrades to
        # "fewer limits enforced", never to "the call fails" — the wall-clock
        # timeout and the allow-list are the guarantees that do not depend on
        # host rlimit support.
        for limit, value in (
            (resource.RLIMIT_CPU, cpu_seconds),
            (resource.RLIMIT_AS, _RLIMIT_AS_BYTES),
            (resource.RLIMIT_NOFILE, _RLIMIT_NOFILE),
            (resource.RLIMIT_FSIZE, _RLIMIT_FSIZE_BYTES),
            (getattr(resource, "RLIMIT_NPROC", None), _RLIMIT_NPROC),
        ):
            if limit is None:
                continue
            try:
                resource.setrlimit(limit, (value, value))
            except (ValueError, OSError):
                pass
        # No os.setsid() here: start_new_session=True (passed to
        # create_subprocess_exec) already does it before preexec_fn runs;
        # calling it again would raise (already a process group leader).

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
            # and checked above; rlimits, the timeout, the output caps and the
            # process-group kill below apply exactly as they do when confined.
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
        return ExecutionResult(
            content=outcome.stdout,
            units=1,
            metadata={
                "exit_code": outcome.exit_code,
                "stderr": outcome.stderr,
                "truncated": outcome.truncated,
                "timed_out": outcome.timed_out,
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

    async def _spawn(
        self, cleaned_argv: list[str], cwd: str, env: Mapping[str, str], timeout: float,
        plan: "confinement.ConfinementPlan | None",
    ) -> ProcessOutcome:
        cpu_seconds = int(timeout) + 5
        try:
            process = await asyncio.create_subprocess_exec(
                *cleaned_argv,
                cwd=cwd,
                env=dict(env),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=lambda: self._child_setup(cpu_seconds, plan),
                pass_fds=(plan.ruleset_fd,) if plan is not None else (),
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise ExecutionError(
                ExecutionErrorCode.INVALID_ARGUMENTS, f"executable not found: {exc}"
            ) from exc
        except subprocess.SubprocessError as exc:
            # The child could not apply its limits/confinement and never exec'd.
            raise ExecutionError(
                ExecutionErrorCode.SANDBOX_VIOLATION, "process confinement could not be applied; nothing was run"
            ) from exc
        except OSError as exc:
            raise ExecutionError(ExecutionErrorCode.INTERNAL, f"failed to start process: {exc}") from exc
        finally:
            if plan is not None:
                plan.close()

        try:
            return await self._communicate_bounded(process, timeout)
        finally:
            await self._ensure_process_group_dead(process)

    async def _communicate_bounded(
        self, process: asyncio.subprocess.Process, timeout: float
    ) -> ProcessOutcome:
        try:
            stdout_task = asyncio.ensure_future(self._read_capped(process.stdout))
            stderr_task = asyncio.ensure_future(self._read_capped(process.stderr))
            done, pending = await asyncio.wait(
                {stdout_task, stderr_task, asyncio.ensure_future(process.wait())},
                timeout=timeout,
            )
            if pending:
                # Timeout: kill the whole group before touching anything else
                # (§11 — a timeout is a deterministic failure, not a hang).
                for task in pending:
                    task.cancel()
                await self._ensure_process_group_dead(process)
                stdout_text, _ = await self._safe_result(stdout_task)
                stderr_text, _ = await self._safe_result(stderr_task)
                return ProcessOutcome(
                    exit_code=-1, stdout=stdout_text, stderr=stderr_text,
                    truncated=True, timed_out=True,
                )
            stdout_text, stdout_truncated = await stdout_task
            stderr_text, _ = await stderr_task
            exit_code = await process.wait()
            return ProcessOutcome(
                exit_code=exit_code, stdout=stdout_text, stderr=stderr_text,
                truncated=stdout_truncated, timed_out=False,
            )
        except asyncio.CancelledError:
            await self._ensure_process_group_dead(process)
            raise

    async def _read_capped(self, stream: asyncio.StreamReader | None) -> tuple[str, bool]:
        if stream is None:
            return "", False
        chunks: list[bytes] = []
        total = 0
        truncated = False
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            if total >= self._max_output_bytes:
                truncated = True
                continue  # keep draining so the child is never blocked on a full pipe
            take = min(len(chunk), self._max_output_bytes - total)
            chunks.append(chunk[:take])
            total += take
            if take < len(chunk):
                truncated = True
        return b"".join(chunks).decode("utf-8", errors="replace"), truncated

    @staticmethod
    async def _safe_result(task: "asyncio.Future") -> tuple[str, bool]:
        try:
            return await task
        except (asyncio.CancelledError, Exception):
            return "", True

    @staticmethod
    async def _ensure_process_group_dead(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        pid = process.pid
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                return
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
                return
            except asyncio.TimeoutError:
                continue


__all__ = ["ConstrainedProcessExecutor", "ProcessOutcome", "argv_digest"]
