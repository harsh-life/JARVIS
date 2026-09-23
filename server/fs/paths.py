"""Path containment (09 §1-§3/§8) — the algorithm the rest of this package
calls, never bypasses.

This is not "check whether the path starts with the sandbox root." That check
alone is exactly what 09 §8 forbids treating as "a security sandbox" — it is
defeated by a symlink and by a classic check-then-use race (TOCTOU): the
string can be safe at validation time and point somewhere else by the time a
syscall touches it.

What this module does instead, on every operation:

1. Reject an absolute path outright — the agent never supplies one (09 §1).
2. Normalize and reject any `..`/empty segment — traversal (09 §2).
3. Reject a NUL byte or a segment that Unicode-normalizes to `.`/`..` — the
   "unicode/percent tricks" 09 §2 names (a fullwidth dot, U+FF0E, collapses
   to ASCII `.` under NFKC and must not sneak a traversal segment through).
4. Walk the path **one directory at a time from an already-open directory
   file descriptor**, opening each intermediate component with
   `O_DIRECTORY | O_NOFOLLOW` relative to the previous hop's fd
   (`dir_fd=`). A symlink anywhere in the path — including one swapped in
   between steps 1-3 and this walk — cannot be silently followed, because
   each hop operates on a file descriptor already known to be a real
   directory, not on a path string re-resolved from scratch. This is the
   TOCTOU defense 09 §2 asks for: there is no window where a check and the
   syscall it guards see different filesystem state, because the "check"
   *is* the syscall that then does the work (`dir_fd`-relative open/stat/
   unlink), not a separate stat beforehand.
5. As a final, cheap belt-and-suspenders check (09 §2 step 4), the resolved
   real path is still verified to be a descendant of the sandbox root before
   any content operation. Steps 1-4 already make this structurally true; this
   is defense in depth, not the primary control.

`[IMPL]` (09 §8, OD-FS-1): this is the **mediated** containment mode — real,
syscall-level, TOCTOU-resistant containment implemented entirely at the
Python/OS-primitive level, because this development environment has no
privilege to create mount namespaces or bind-mount a per-tool root. 09 §8's
`[REC]` stronger mechanism — an isolated execution context whose only
writable mount *is* the sandbox root, so even a tool that ignores this module
entirely has nothing else to reach — is real infrastructure work for a
production deployment, not shipped by this branch. `server/config/schema.py`'s
`containment_mode` field names this honestly; `sandbox.py` refuses to start in
`mount_isolated` mode rather than silently running the weaker mode under a
stronger-sounding name.
"""

from __future__ import annotations

import errno
import os
import stat
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import UUID

from shared.schemas.execution import ExecutionError, ExecutionErrorCode

_MAX_SEGMENTS = 64  # a path this deep inside a sandbox is already pathological
_MAX_LABEL_LENGTH = 128
_MAX_SEGMENT_LENGTH = 255


def _forbidden(message: str) -> ExecutionError:
    return ExecutionError(ExecutionErrorCode.FORBIDDEN_PATH, message)


def sanitize_label(label: str) -> str:
    """A `resource_scope["sandbox_root"]` value is a caller-chosen *label*,
    never a filesystem path (09 §1: the physical location is *derived*, never
    taken raw). A label that looks like a path — contains `/`, `\\`, a NUL
    byte, or is exactly `.`/`..` — is refused outright rather than silently
    reinterpreted, so a grant can never smuggle a raw path through this field
    (see `server/fs/__init__.py` for why this is stricter than 09's own
    illustrative `"sandbox_root": "/…"` example).
    """

    if not label or len(label) > _MAX_LABEL_LENGTH:
        raise _forbidden("sandbox_root scope label is empty or too long")
    if "\x00" in label or "/" in label or "\\" in label:
        raise _forbidden("sandbox_root scope label must not contain a path separator")
    normalized = unicodedata.normalize("NFKC", label)
    if normalized in (".", "..") or normalized != label.strip():
        raise _forbidden("sandbox_root scope label is not a plain name")
    return label


def _segments(relative_path: str) -> list[str]:
    if not isinstance(relative_path, str) or not relative_path:
        raise _forbidden("a relative path is required")
    if "\x00" in relative_path:
        raise _forbidden("path contains a NUL byte")
    if relative_path.startswith("/") or relative_path.startswith("\\"):
        raise _forbidden("absolute paths are never accepted from a tool (09 §1)")
    if len(relative_path) >= 2 and relative_path[1] == ":":
        raise _forbidden("drive-qualified paths are never accepted from a tool")

    normalized = unicodedata.normalize("NFKC", relative_path)
    parts = [p for p in normalized.replace("\\", "/").split("/")]
    resolved: list[str] = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            # 09 §2: reject outright rather than "resolve up and see" — a
            # sandbox root has no parent a tool is ever allowed to reach.
            raise _forbidden("'..' path traversal is rejected")
        if len(part) > _MAX_SEGMENT_LENGTH:
            raise _forbidden("path segment too long")
        resolved.append(part)

    if not resolved:
        raise _forbidden("path resolves to the sandbox root itself, not a file")
    if len(resolved) > _MAX_SEGMENTS:
        raise _forbidden("path is too deep")
    return resolved


@contextmanager
def _open_fd(path: str, flags: int, *, dir_fd: int | None = None, mode: int = 0) -> Iterator[int]:
    fd = os.open(path, flags, mode, dir_fd=dir_fd)
    try:
        yield fd
    finally:
        os.close(fd)


def open_root_fd(root_real: str) -> int:
    """Public seam for a caller (`sandbox.py`'s directory listing at the
    sandbox root) that needs the root's own fd rather than a fd reached by
    walking through it. Caller must `os.close` the result."""

    return _root_fd(root_real)


def _root_fd(root_real: str) -> int:
    try:
        return os.open(root_real, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise ExecutionError(
            ExecutionErrorCode.INTERNAL, f"sandbox root is not a directory: {exc}"
        ) from exc


def _is_symlink_at(dir_fd: int, name: str) -> bool:
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode)


def _open_next(dir_fd: int, name: str, *, directory: bool, create: bool = False) -> int:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if directory:
        flags |= os.O_DIRECTORY
    try:
        return os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _forbidden(
                "a symlink inside the sandbox is never followed (09 §3)"
            ) from exc
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            # A directory hop refused with O_DIRECTORY|O_NOFOLLOW can surface
            # as ENOTDIR rather than ELOOP when the entry is a symlink,
            # depending on the kernel — disambiguate with an explicit lstat
            # rather than trusting the errno alone, so a symlinked
            # intermediate directory is still classified and rejected as
            # FORBIDDEN_PATH (09 §3), not waved through as "just missing".
            if directory and _is_symlink_at(dir_fd, name):
                raise _forbidden(
                    "a symlink inside the sandbox is never followed (09 §3)"
                ) from exc
            if create and directory and exc.errno == errno.ENOENT:
                try:
                    os.mkdir(name, 0o700, dir_fd=dir_fd)
                except FileExistsError:
                    pass  # raced with something else creating it; retry open
                except OSError as mkdir_exc:
                    raise _forbidden(
                        f"cannot create directory inside the sandbox: {mkdir_exc}"
                    ) from mkdir_exc
                return _open_next(dir_fd, name, directory=directory, create=False)
            raise ExecutionError(
                ExecutionErrorCode.INVALID_ARGUMENTS, "no such file inside the sandbox"
            ) from exc
        raise _forbidden(f"cannot open path segment inside the sandbox: {exc}") from exc


@contextmanager
def _parent_dir_fd(root_fd: int, parts: list[str], *, create_parents: bool) -> Iterator[int]:
    """Walk every segment but the last, `O_NOFOLLOW` at each hop, and yield an
    fd for the final parent directory. This is the TOCTOU-resistant core of
    the module: each hop is itself the containment check (§0).

    `create_parents` (write-family operations only) creates a missing
    intermediate directory *at that hop*, via `mkdir(dir_fd=...)` relative to
    the already-verified parent fd — never by re-resolving a path string —
    so directory creation gets exactly the same containment guarantee as
    every other operation.
    """

    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            nxt = _open_next(current, part, directory=True, create=create_parents)
            os.close(current)
            current = nxt
        yield current
    finally:
        os.close(current)


def _assert_realpath_contained(root_real: str, dir_fd: int, name: str) -> None:
    """Belt-and-suspenders (09 §2 step 4): even though the walk above cannot
    be tricked by a symlink, independently confirm the fd we are about to
    operate through corresponds to a real path under the root."""

    try:
        resolved = os.path.realpath(f"/proc/self/fd/{dir_fd}")
    except OSError:
        return  # /proc unavailable (non-Linux); the dir_fd walk already holds.
    root_with_sep = root_real.rstrip(os.sep) + os.sep
    if not (resolved == root_real or resolved.startswith(root_with_sep)):
        raise _forbidden("resolved path escaped the sandbox root")


class SandboxPath:
    """A validated (parent_dir_fd, final_segment) pair — everything a
    content operation needs, and nothing it can use to reach outside the
    root. `root_real` is retained only for the belt-and-suspenders check and
    for building a human-readable resource reference for audit/errors, never
    reused to re-resolve a path from a string.
    """

    __slots__ = ("_root_fd", "_parent_fd", "name", "root_real", "display")

    def __init__(self, root_fd: int, parent_fd: int, name: str, root_real: str, display: str) -> None:
        self._root_fd = root_fd
        self._parent_fd = parent_fd
        self.name = name
        self.root_real = root_real
        self.display = display

    @property
    def parent_fd(self) -> int:
        return self._parent_fd


@contextmanager
def resolve(root_real: str, relative_path: str, *, create_parents: bool = False) -> Iterator[SandboxPath]:
    """The single entry point every fs operation in `sandbox.py` uses.

    Yields a `SandboxPath` good for the lifetime of the `with` block; the
    parent directory fd is closed on exit. Never returns a bare string path —
    that would reopen the TOCTOU window this module exists to close.

    `create_parents` is for write-family operations only (`sandbox.py`'s
    `_write`) — a missing intermediate directory is created at the hop that
    needs it, through the same `dir_fd`-relative primitive as everything
    else, never by pre-creating a whole path from a string.
    """

    parts = _segments(relative_path)
    root_fd = _root_fd(root_real)
    try:
        with _parent_dir_fd(root_fd, parts, create_parents=create_parents) as parent_fd:
            _assert_realpath_contained(root_real, parent_fd, parts[-1])
            yield SandboxPath(root_fd, parent_fd, parts[-1], root_real, relative_path)
    finally:
        os.close(root_fd)


def allocate_root(
    base_root: str,
    *,
    user_id: UUID,
    graph_id: UUID | None,
    label: str,
) -> str:
    """09 §1's layout: `…/users/{user_id}/private/{label}` or
    `…/graphs/{graph_id}/shared/{label}`. The physical root is always nested
    under the *authorized, server-derived* `user_id`/`graph_id` — never under
    a value the request supplies — so a principal's own sandbox derivation
    structurally cannot produce another principal's root (09 §6). `label` is
    the caller-chosen (and sanitized) name *within* that principal's own
    area; it can select which sub-sandbox to use, never whose.
    """

    safe_label = sanitize_label(label)
    base = Path(base_root)
    if graph_id is not None:
        target = base / "graphs" / str(graph_id) / "shared" / safe_label
    else:
        target = base / "users" / str(user_id) / "private" / safe_label
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Directories created before this call (e.g. an ancestor made by a
    # different label) may already be more permissive from an earlier mkdir;
    # re-assert least-privilege on the leaf we actually hand out.
    os.chmod(target, 0o700)
    return str(target.resolve(strict=True))


def task_temp_root(base_root: str, *, task_id: UUID) -> str:
    """09 §7: task-scoped temp, cleaned up at task end / by a periodic sweep
    (`sweep_stale_task_temp`), never shared across tasks."""

    target = Path(base_root) / "tasks" / str(task_id) / "tmp"
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target, 0o700)
    return str(target.resolve(strict=True))


def cleanup_task_temp(base_root: str, *, task_id: UUID) -> None:
    """Remove one task's temp directory entirely. Idempotent — a task that
    never touched the filesystem has nothing to remove."""

    import shutil

    target = Path(base_root) / "tasks" / str(task_id)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


__all__ = [
    "SandboxPath",
    "allocate_root",
    "cleanup_task_temp",
    "open_root_fd",
    "resolve",
    "sanitize_label",
    "task_temp_root",
]
