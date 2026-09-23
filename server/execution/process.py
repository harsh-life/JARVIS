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
  outlive the call that spawned it.
"""

from __future__ import annotations

import asyncio
import os
import resource
import shutil
import signal
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from shared.schemas.execution import ExecutionError, ExecutionErrorCode, ExecutionResult

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

_MAX_ARGV_LENGTH = 256
_MAX_ARG_BYTES = 4096

# Conservative, fixed rlimit defaults (POSIX only). Not exposed as config in
# this pass — existence and enforcement is what §4 locks; exact tuning is an
# operator concern once real workloads exist.
_RLIMIT_AS_BYTES = 512 * 1024 * 1024
_RLIMIT_NOFILE = 64
_RLIMIT_NPROC = 32


@dataclass(frozen=True)
class ProcessOutcome:
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool


class ConstrainedProcessExecutor:
    """One instance per process, configured from `ExecutionConfig.process`."""

    def __init__(
        self,
        *,
        allowed_executables: Sequence[str] = (),
        default_timeout_seconds: float = 10.0,
        max_timeout_seconds: float = 60.0,
        max_output_bytes: int = 1_000_000,
    ) -> None:
        self._allowed = frozenset(allowed_executables)
        # A bare (no "/") allow-list entry is resolved *once*, here, against
        # the exact fixed PATH the child gets (_BASE_ENV["PATH"]) — never
        # against the host's ambient $PATH — to the one absolute path it
        # names today. That resolved path, plus the bare name itself (so a
        # PATH-searched invocation still works), are the only two spellings
        # accepted for that entry. Security review finding: matching on
        # `os.path.basename(argv[0])` alone (a bare-name entry accepting
        # *any* absolute path sharing that basename) let an operator who
        # allow-listed "python3" unintentionally authorize an unrelated
        # binary at a different path with the same basename — an
        # `execve`-semantics bypass ("/"-containing argv[0] never goes
        # through PATH search, so nothing constrained which such path could
        # be supplied). Resolving to one concrete path closes that without
        # losing the ergonomics of writing a bare name in config.
        self._resolved_allowed = frozenset(
            resolved
            for name in allowed_executables
            if "/" not in name
            for resolved in (shutil.which(name, path=_BASE_ENV["PATH"]),)
            if resolved
        )
        self._default_timeout = default_timeout_seconds
        self._max_timeout = max_timeout_seconds
        self._max_output_bytes = max_output_bytes

    def _assert_allowed(self, executable: str) -> None:
        # Exact match only — a bare allow-list entry additionally matches
        # the one absolute path it resolved to at construction time
        # (`self._resolved_allowed`), never any other path that merely
        # shares its basename. Every accepted spelling is one the operator
        # either wrote down or that PATH resolution deterministically named.
        if executable in self._allowed or executable in self._resolved_allowed:
            return
        raise ExecutionError(
            ExecutionErrorCode.UNAUTHORIZED_EXECUTABLE,
            f"{executable!r} is not in the configured process allow-list",
        )

    def _build_argv(self, argv: Sequence[str]) -> list[str]:
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
        self._assert_allowed(cleaned[0])
        return cleaned

    def _build_env(self, overrides: Mapping[str, str] | None) -> dict[str, str]:
        env = dict(_BASE_ENV)
        for key, value in (overrides or {}).items():
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
    ) -> ExecutionResult:
        cleaned_argv = self._build_argv(argv)
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

        try:
            process = await asyncio.create_subprocess_exec(
                *cleaned_argv,
                cwd=cwd,
                env=child_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=lambda: self._set_child_rlimits(int(effective_timeout) + 5),
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise ExecutionError(
                ExecutionErrorCode.INVALID_ARGUMENTS, f"executable not found: {exc}"
            ) from exc
        except OSError as exc:
            raise ExecutionError(ExecutionErrorCode.INTERNAL, f"failed to start process: {exc}") from exc

        try:
            outcome = await self._communicate_bounded(process, effective_timeout)
        finally:
            await self._ensure_process_group_dead(process)

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


__all__ = ["ConstrainedProcessExecutor", "ProcessOutcome"]
