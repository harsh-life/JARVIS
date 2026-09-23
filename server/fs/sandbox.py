"""High-level sandboxed filesystem operations (09 §1/§4/§5/§6/§7).

Every function here takes an already-authorized (`user_id`, `graph_id`,
`sandbox_root` label, `relative_path`) tuple — the caller (`server/tools/
platforms.py`'s file adapters) builds this from an `ExecutionRequest`, which
in turn only exists because `04`/`07` already authorized the operation. This
module does not re-check capability, visibility, or ownership: it enforces
the *physical* boundary (09), not the authorization decision (04/07) — see
the package docstring in `server/fs/__init__.py`.
"""

from __future__ import annotations

import errno
import os
import stat as stat_module
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from shared.schemas.execution import ExecutionError, ExecutionErrorCode, ExecutionResult
from server.fs import paths
from server.fs.paths import SandboxPath

def _read_capped(fileobj: BinaryIO, cap: int) -> bytes:
    """Read at most `cap` bytes, raising rather than allocating unbounded
    memory for a stream whose declared size undersells what it decompresses
    to (the honest defense a zip/tar-bomb test actually needs, §4)."""

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = fileobj.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise ExecutionError(
                ExecutionErrorCode.RESOURCE_EXHAUSTED, "archive entry exceeds the per-file cap"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _reject_absolute_archive_entry(name: str) -> None:
    """09 §4: an entry naming an absolute path is refused explicitly, not
    merely relied upon to be neutralized by how `dest` happens to be built —
    prepending `dest_relative` to an absolute-looking entry name defangs it
    today only because string concatenation, not `os.path.join`, is used
    below; that is an implementation detail a future edit could innocently
    invert. Rejecting the entry outright removes the dependency on it.
    """

    if name.startswith("/") or name.startswith("\\") or (len(name) >= 2 and name[1] == ":"):
        raise ExecutionError(
            ExecutionErrorCode.FORBIDDEN_PATH, "archive entry has an absolute path — refused"
        )


_SENSITIVE_HOST_PATHS = (
    "/root",
    "/etc",
    "/proc",
    "/sys",
    "/boot",
    "/var/lib/hypermind/secrets",
)


@dataclass(frozen=True)
class DirEntryInfo:
    name: str
    is_dir: bool
    size_bytes: int


class FilesystemSandbox:
    """One instance per process, configured once from `ExecutionConfig.filesystem`.

    Stateless beyond its configuration — every call re-derives and
    re-validates its own path (paths.resolve), so there is nothing here a
    concurrent call could corrupt for another.
    """

    def __init__(
        self,
        *,
        base_root: str,
        containment_mode: str = "mediated",
        max_file_bytes: int = 25_000_000,
        max_sandbox_bytes: int = 250_000_000,
        max_archive_entries: int = 10_000,
        max_archive_uncompressed_bytes: int = 250_000_000,
    ) -> None:
        if containment_mode != "mediated":
            # 09 §8 / OD-FS-1: refuse to silently run a weaker mode under a
            # stronger-sounding config value — "mount_isolated" is not
            # implemented by this branch (see server/fs/paths.py module
            # docstring). Fail closed at construction, not at the first call.
            raise ExecutionError(
                ExecutionErrorCode.INTERNAL,
                f"filesystem containment_mode {containment_mode!r} is not implemented by "
                "this branch — only 'mediated' is available; do not represent an "
                "unimplemented stronger mode as configured",
            )
        self._base_root = str(Path(base_root).resolve())
        # Validate *before* touching the filesystem, not after: checking
        # post-mkdir meant a permission-denied host path failed with a raw
        # PermissionError instead of this module's own ExecutionError (CI
        # caught this running as a non-root user; a dev environment running
        # as root masked it, since root can mkdir almost anywhere) — and,
        # worse, on a host where the process *does* have permission, the
        # directory would already have been created at the forbidden
        # location before the rejection ever fired.
        self._assert_not_sensitive(self._base_root)
        Path(self._base_root).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_file_bytes = max_file_bytes
        self.max_sandbox_bytes = max_sandbox_bytes
        self.max_archive_entries = max_archive_entries
        self.max_archive_uncompressed_bytes = max_archive_uncompressed_bytes

    @staticmethod
    def _assert_not_sensitive(root: str) -> None:
        # FS-003: structural, not a denylist substitute — the base_root an
        # operator configures must not itself coincide with a sensitive host
        # path. Every sandbox root is derived under this one directory
        # (paths.allocate_root), so this single check protects every tenant.
        for sensitive in _SENSITIVE_HOST_PATHS:
            if root == sensitive or root.startswith(sensitive.rstrip("/") + "/"):
                raise ExecutionError(
                    ExecutionErrorCode.INTERNAL,
                    f"filesystem.base_root must not be under {sensitive} (FS-003)",
                )

    # ── root allocation ─────────────────────────────────────────────────

    def root_for(self, *, user_id: UUID, graph_id: UUID | None, label: str) -> str:
        return paths.allocate_root(self._base_root, user_id=user_id, graph_id=graph_id, label=label)

    def task_temp(self, *, task_id: UUID) -> str:
        return paths.task_temp_root(self._base_root, task_id=task_id)

    def cleanup_task(self, *, task_id: UUID) -> None:
        paths.cleanup_task_temp(self._base_root, task_id=task_id)

    # ── quota ────────────────────────────────────────────────────────────

    def _sandbox_usage(self, root_real: str) -> int:
        total = 0
        for dirpath, dirnames, filenames in os.walk(root_real, followlinks=False):
            for filename in filenames:
                full = os.path.join(dirpath, filename)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if stat_module.S_ISREG(st.st_mode):
                    total += st.st_size
        return total

    # ── read ─────────────────────────────────────────────────────────────

    def read_file(self, root_real: str, relative_path: str) -> ExecutionResult:
        with paths.resolve(root_real, relative_path) as target:
            try:
                st = os.stat(target.name, dir_fd=target.parent_fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise ExecutionError(
                    ExecutionErrorCode.INVALID_ARGUMENTS, "file does not exist"
                ) from exc
            if stat_module.S_ISLNK(st.st_mode):
                raise ExecutionError(
                    ExecutionErrorCode.FORBIDDEN_PATH, "refusing to read through a symlink"
                )
            if not stat_module.S_ISREG(st.st_mode):
                raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, "not a regular file")
            if st.st_size > self.max_file_bytes:
                raise ExecutionError(
                    ExecutionErrorCode.RESOURCE_EXHAUSTED,
                    f"file exceeds the {self.max_file_bytes}-byte read cap",
                )
            fd = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=target.parent_fd)
            try:
                with os.fdopen(fd, "rb", closefd=True) as handle:
                    data = handle.read(self.max_file_bytes + 1)
            except OSError as exc:
                raise ExecutionError(ExecutionErrorCode.INTERNAL, str(exc)) from exc
            if len(data) > self.max_file_bytes:
                raise ExecutionError(ExecutionErrorCode.RESOURCE_EXHAUSTED, "file grew during read")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("utf-8", errors="replace")
            return ExecutionResult(content=text, units=1, metadata={"size_bytes": len(data)})

    def stat(self, root_real: str, relative_path: str) -> ExecutionResult:
        with paths.resolve(root_real, relative_path) as target:
            try:
                st = os.stat(target.name, dir_fd=target.parent_fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise ExecutionError(
                    ExecutionErrorCode.INVALID_ARGUMENTS, "path does not exist"
                ) from exc
            kind = (
                "symlink" if stat_module.S_ISLNK(st.st_mode)
                else "directory" if stat_module.S_ISDIR(st.st_mode)
                else "file" if stat_module.S_ISREG(st.st_mode)
                else "other"
            )
            return ExecutionResult(
                content=f"{kind} {st.st_size} bytes",
                metadata={"kind": kind, "size_bytes": st.st_size},
            )

    def list_directory(self, root_real: str, relative_path: str = ".") -> ExecutionResult:
        parts_display = relative_path
        if relative_path in (".", ""):
            fd = paths.open_root_fd(root_real)
            try:
                entries = self._scan(fd)
            finally:
                os.close(fd)
        else:
            with paths.resolve(root_real, relative_path) as target:
                try:
                    st = os.stat(target.name, dir_fd=target.parent_fd, follow_symlinks=False)
                except FileNotFoundError as exc:
                    raise ExecutionError(
                        ExecutionErrorCode.INVALID_ARGUMENTS, "directory does not exist"
                    ) from exc
                if not stat_module.S_ISDIR(st.st_mode):
                    raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, "not a directory")
                fd = os.open(target.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=target.parent_fd)
                try:
                    entries = self._scan(fd)
                finally:
                    os.close(fd)
        names = ", ".join(f"{e.name}{'/' if e.is_dir else ''}" for e in entries) or "(empty)"
        return ExecutionResult(content=names, metadata={"count": len(entries)})

    @staticmethod
    def _scan(dir_fd: int) -> list[DirEntryInfo]:
        out: list[DirEntryInfo] = []
        for name in os.listdir(dir_fd):
            try:
                st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError:
                continue
            if stat_module.S_ISLNK(st.st_mode):
                continue  # never surface a symlink as a traversable entry
            out.append(DirEntryInfo(name=name, is_dir=stat_module.S_ISDIR(st.st_mode), size_bytes=st.st_size))
        return sorted(out, key=lambda e: e.name)

    # ── write ────────────────────────────────────────────────────────────

    def _quota_root(self, root_real: str) -> str:
        """The directory a quota is charged against: the *principal's* whole
        area (`users/{user_id}/private` or `graphs/{graph_id}/shared`), not the
        one label being written.

        A label is caller-chosen (`sanitize_label`), so a per-label quota is a
        per-anything quota: a principal could fill `max_sandbox_bytes` under as
        many labels as it cared to name. Charging the parent area makes the
        limit a real per-principal storage bound (13 §2 / 09 §7).
        """

        root = Path(root_real)
        try:
            relative = root.relative_to(self._base_root)
        except ValueError:
            return root_real
        parts = relative.parts
        if len(parts) == 4 and (parts[0], parts[2]) in {("users", "private"), ("graphs", "shared")}:
            return str(root.parent)
        return root_real

    def _check_quota(self, root_real: str, incoming_bytes: int) -> None:
        if self._sandbox_usage(self._quota_root(root_real)) + incoming_bytes > self.max_sandbox_bytes:
            raise ExecutionError(
                ExecutionErrorCode.RESOURCE_EXHAUSTED,
                f"write would exceed the sandbox quota ({self.max_sandbox_bytes} bytes)",
            )

    def _write(self, root_real: str, relative_path: str, content: str, *, must_not_exist: bool) -> ExecutionResult:
        payload = content.encode("utf-8")
        if len(payload) > self.max_file_bytes:
            raise ExecutionError(
                ExecutionErrorCode.RESOURCE_EXHAUSTED,
                f"write exceeds the {self.max_file_bytes}-byte file cap",
            )
        self._check_quota(root_real, len(payload))
        # Write-family operations create a missing intermediate directory as
        # they walk to it (e.g. archive extraction into a fresh subtree);
        # read/stat/delete never do — a missing parent there is just "not
        # found", not something to conjure into existence.
        with paths.resolve(root_real, relative_path, create_parents=True) as target:
            flags = os.O_WRONLY | os.O_NOFOLLOW | (os.O_CREAT | os.O_EXCL if must_not_exist else os.O_TRUNC)
            if not must_not_exist:
                try:
                    st = os.stat(target.name, dir_fd=target.parent_fd, follow_symlinks=False)
                except FileNotFoundError as exc:
                    raise ExecutionError(
                        ExecutionErrorCode.INVALID_ARGUMENTS,
                        "write_file requires an existing file (use create_file for a new one)",
                    ) from exc
                if stat_module.S_ISLNK(st.st_mode):
                    raise ExecutionError(
                        ExecutionErrorCode.FORBIDDEN_PATH, "refusing to write through a symlink"
                    )
                if not stat_module.S_ISREG(st.st_mode):
                    raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, "not a regular file")
            try:
                fd = os.open(target.name, flags, 0o600, dir_fd=target.parent_fd)
            except FileExistsError as exc:
                raise ExecutionError(
                    ExecutionErrorCode.INVALID_ARGUMENTS, "create_file: a file already exists there"
                ) from exc
            except OSError as exc:
                if exc.errno == errno.ELOOP:  # symlink swapped in after the pre-check
                    raise ExecutionError(
                        ExecutionErrorCode.FORBIDDEN_PATH, "refusing to write through a symlink"
                    ) from exc
                raise ExecutionError(
                    ExecutionErrorCode.INVALID_ARGUMENTS, f"cannot open file for write: {exc}"
                ) from exc
            try:
                with os.fdopen(fd, "wb", closefd=True) as handle:
                    handle.write(payload)
            except OSError as exc:
                raise ExecutionError(ExecutionErrorCode.INTERNAL, str(exc)) from exc
            return ExecutionResult(content="written", units=1, metadata={"size_bytes": len(payload)})

    def create_file(self, root_real: str, relative_path: str, content: str = "") -> ExecutionResult:
        return self._write(root_real, relative_path, content, must_not_exist=True)

    def write_file(self, root_real: str, relative_path: str, content: str) -> ExecutionResult:
        return self._write(root_real, relative_path, content, must_not_exist=False)

    def delete_file(self, root_real: str, relative_path: str) -> ExecutionResult:
        with paths.resolve(root_real, relative_path) as target:
            try:
                st = os.stat(target.name, dir_fd=target.parent_fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise ExecutionError(
                    ExecutionErrorCode.INVALID_ARGUMENTS, "file does not exist"
                ) from exc
            if not stat_module.S_ISREG(st.st_mode):
                raise ExecutionError(
                    ExecutionErrorCode.FORBIDDEN_PATH,
                    "delete_file only removes a regular file (bulk_delete for a subtree)",
                )
            os.unlink(target.name, dir_fd=target.parent_fd)
            return ExecutionResult(content="deleted")

    def bulk_delete(self, root_real: str, relative_path: str) -> ExecutionResult:
        with paths.resolve(root_real, relative_path) as target:
            removed = self._remove_tree(target.parent_fd, target.name, depth=0)
            return ExecutionResult(content=f"removed {removed} entries", metadata={"removed": removed})

    def _remove_tree(self, dir_fd: int, name: str, *, depth: int) -> int:
        if depth > 64:
            raise ExecutionError(ExecutionErrorCode.RESOURCE_EXHAUSTED, "subtree too deep")
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, "path does not exist") from exc
        if stat_module.S_ISLNK(st.st_mode):
            raise ExecutionError(ExecutionErrorCode.FORBIDDEN_PATH, "refusing to remove through a symlink")
        if stat_module.S_ISREG(st.st_mode):
            os.unlink(name, dir_fd=dir_fd)
            return 1
        if not stat_module.S_ISDIR(st.st_mode):
            raise ExecutionError(ExecutionErrorCode.FORBIDDEN_PATH, "refusing to remove a non-regular entry")
        count = 0
        sub_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
        try:
            for entry in os.listdir(sub_fd):
                count += self._remove_tree(sub_fd, entry, depth=depth + 1)
        finally:
            os.close(sub_fd)
        os.rmdir(name, dir_fd=dir_fd)
        return count + 1

    # ── archive extraction (09 §4 — zip-slip / zip-bomb defense) ───────────

    def extract_zip(self, root_real: str, archive_path: str, dest_relative: str) -> ExecutionResult:
        with paths.resolve(root_real, archive_path) as archive:
            fd = os.open(archive.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=archive.parent_fd)
            try:
                with os.fdopen(fd, "rb", closefd=True) as handle:
                    return self._extract_zip_from(handle, root_real, dest_relative)
            except zipfile.BadZipFile as exc:
                raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, f"not a zip file: {exc}") from exc

    def _extract_zip_from(self, handle: BinaryIO, root_real: str, dest_relative: str) -> ExecutionResult:
        with zipfile.ZipFile(handle) as archive:
            infos = archive.infolist()
            self._check_archive_caps(len(infos), sum(i.file_size for i in infos))
            extracted = 0
            for info in infos:
                if info.is_dir():
                    continue
                if self._is_symlink_zip_entry(info):
                    raise ExecutionError(
                        ExecutionErrorCode.FORBIDDEN_PATH, "archive entry is a symlink — refused"
                    )
                _reject_absolute_archive_entry(info.filename)
                # The zip-slip defense: every entry's destination goes through
                # the exact same `paths.resolve` containment as any other
                # write — an entry named `../../evil` is rejected here, not
                # written, because `paths._segments` refuses `..` (09 §4).
                dest = f"{dest_relative.rstrip('/')}/{info.filename}"
                # A stream-read with a hard cap, not `.read()` — a crafted
                # entry's *declared* size (already checked above) can lie
                # about how much data actually decompresses (09 §4's
                # zip-bomb defense has to hold against that, not just against
                # an honest central directory).
                with archive.open(info) as entry:
                    data = _read_capped(entry, self.max_file_bytes)
                self.create_file(root_real, dest, data.decode("utf-8", errors="surrogateescape"))
                extracted += 1
            return ExecutionResult(content=f"extracted {extracted} entries", metadata={"count": extracted})

    def extract_tar(self, root_real: str, archive_path: str, dest_relative: str) -> ExecutionResult:
        with paths.resolve(root_real, archive_path) as archive:
            fd = os.open(archive.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=archive.parent_fd)
            with os.fdopen(fd, "rb", closefd=True) as handle:
                try:
                    with tarfile.open(fileobj=handle) as tar:
                        return self._extract_tar_from(tar, root_real, dest_relative)
                except tarfile.TarError as exc:
                    raise ExecutionError(
                        ExecutionErrorCode.INVALID_ARGUMENTS, f"not a valid tar file: {exc}"
                    ) from exc

    def _extract_tar_from(self, tar: tarfile.TarFile, root_real: str, dest_relative: str) -> ExecutionResult:
        members = tar.getmembers()
        self._check_archive_caps(len(members), sum(max(0, m.size) for m in members))
        extracted = 0
        for member in members:
            if member.issym() or member.islnk():
                raise ExecutionError(ExecutionErrorCode.FORBIDDEN_PATH, "archive entry is a link — refused")
            if member.isdev():
                raise ExecutionError(
                    ExecutionErrorCode.FORBIDDEN_PATH, "archive entry is a device/special file — refused"
                )
            if not member.isfile():
                continue
            _reject_absolute_archive_entry(member.name)
            dest = f"{dest_relative.rstrip('/')}/{member.name}"
            extracted_file = tar.extractfile(member)
            data = _read_capped(extracted_file, self.max_file_bytes) if extracted_file is not None else b""
            self.create_file(root_real, dest, data.decode("utf-8", errors="surrogateescape"))
            extracted += 1
        return ExecutionResult(content=f"extracted {extracted} entries", metadata={"count": extracted})

    def _check_archive_caps(self, entry_count: int, uncompressed_total: int) -> None:
        if entry_count > self.max_archive_entries:
            raise ExecutionError(
                ExecutionErrorCode.RESOURCE_EXHAUSTED,
                f"archive has {entry_count} entries, over the {self.max_archive_entries} cap",
            )
        if uncompressed_total > self.max_archive_uncompressed_bytes:
            raise ExecutionError(
                ExecutionErrorCode.RESOURCE_EXHAUSTED,
                "archive's declared uncompressed size exceeds the cap (zip-bomb defense)",
            )

    @staticmethod
    def _is_symlink_zip_entry(info: zipfile.ZipInfo) -> bool:
        # The upper 16 bits of external_attr are the Unix st_mode when the
        # archive was created on a Unix system (the common case for a
        # malicious zip-slip payload); a symlink entry's mode bits are
        # S_IFLNK. Archives without Unix attrs (external_attr's high bits
        # zero) cannot encode a symlink this way and pass through unflagged.
        unix_mode = info.external_attr >> 16
        return stat_module.S_ISLNK(unix_mode) if unix_mode else False


__all__ = ["DirEntryInfo", "FilesystemSandbox"]
