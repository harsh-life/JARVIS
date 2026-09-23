"""server/fs — 09_FILESYSTEM_SANDBOX.md's acceptance hooks (FS-T1..FS-T11).

These exercise the real `FilesystemSandbox` against a real temp directory —
no mock filesystem, no stubbed `os` calls — because 09 §8 is explicit that a
path-checking wrapper is not what this package is allowed to call a sandbox;
the only way to prove otherwise is to actually attempt the escape.
"""

from __future__ import annotations

import os
import shutil
import stat as stat_module
import tarfile
import uuid
import zipfile
from pathlib import Path

import pytest

from server.fs import FilesystemSandbox
from shared.schemas.execution import ExecutionError, ExecutionErrorCode


@pytest.fixture
def sandbox(tmp_path) -> FilesystemSandbox:
    return FilesystemSandbox(base_root=str(tmp_path / "sandboxes"))


@pytest.fixture
def user_root(sandbox: FilesystemSandbox) -> tuple[FilesystemSandbox, str, uuid.UUID]:
    user_id = uuid.uuid4()
    root = sandbox.root_for(user_id=user_id, graph_id=None, label="notes")
    return sandbox, root, user_id


def _error(excinfo) -> ExecutionErrorCode:
    return excinfo.value.code


# ── FS-T1: traversal ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        "../../etc/passwd",
        "../../../etc/shadow",
        "a/../../b",
        "..\\..\\windows\\system32",
        "sub/../../escape",
    ],
)
def test_traversal_is_rejected(user_root, payload):
    sandbox, root, _ = user_root
    with pytest.raises(ExecutionError) as excinfo:
        sandbox.read_file(root, payload)
    assert _error(excinfo) == ExecutionErrorCode.FORBIDDEN_PATH


def test_absolute_path_is_rejected(user_root):
    sandbox, root, _ = user_root
    with pytest.raises(ExecutionError) as excinfo:
        sandbox.read_file(root, "/etc/passwd")
    assert _error(excinfo) == ExecutionErrorCode.FORBIDDEN_PATH


def test_bare_root_reference_is_rejected(user_root):
    sandbox, root, _ = user_root
    for payload in (".", "", "./"):
        with pytest.raises(ExecutionError):
            sandbox.read_file(root, payload)


def test_unicode_dot_trick_does_not_smuggle_traversal(user_root):
    """A fullwidth dot (U+FF0E) NFKC-normalizes to ASCII '.', so
    '．．/escape' must be caught exactly like '../escape'."""

    sandbox, root, _ = user_root
    with pytest.raises(ExecutionError) as excinfo:
        sandbox.read_file(root, "．．/escape")
    assert _error(excinfo) == ExecutionErrorCode.FORBIDDEN_PATH


# ── FS-T2 / FS-T9: symlink escape, including a real TOCTOU attempt ─────


def test_symlink_pointing_outside_sandbox_is_rejected(user_root):
    sandbox, root, _ = user_root
    outside = Path(root).parent / "outside_secret.txt"
    outside.write_text("host secret")
    os.symlink(outside, os.path.join(root, "escape_link"))

    with pytest.raises(ExecutionError) as excinfo:
        sandbox.read_file(root, "escape_link")
    assert _error(excinfo) == ExecutionErrorCode.FORBIDDEN_PATH


def test_symlink_to_sensitive_host_path_is_rejected(user_root):
    sandbox, root, _ = user_root
    os.symlink("/etc/passwd", os.path.join(root, "passwd_link"))
    with pytest.raises(ExecutionError):
        sandbox.read_file(root, "passwd_link")


def test_symlinked_intermediate_directory_is_rejected(user_root):
    """A symlink is not only dangerous as the final component — a directory
    *in the middle* of the path must also never be followed."""

    sandbox, root, _ = user_root
    outside_dir = Path(root).parent / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("nope")
    os.symlink(outside_dir, os.path.join(root, "linked_dir"))

    with pytest.raises(ExecutionError) as excinfo:
        sandbox.read_file(root, "linked_dir/secret.txt")
    assert _error(excinfo) == ExecutionErrorCode.FORBIDDEN_PATH


def test_symlink_swapped_in_after_a_hypothetical_check_is_still_caught(user_root):
    """The real TOCTOU test (FS-T9): create a legitimate regular file, then
    *replace* it with a symlink to a host secret before the read — proving
    containment is enforced by the syscall that does the work (dir_fd-relative
    open with O_NOFOLLOW), not by a separate stat performed earlier and
    trusted."""

    sandbox, root, _ = user_root
    target = os.path.join(root, "swapped.txt")
    with open(target, "w") as handle:
        handle.write("originally fine")

    outside = Path(root).parent / "outside_secret2.txt"
    outside.write_text("host secret 2")
    os.unlink(target)
    os.symlink(outside, target)

    with pytest.raises(ExecutionError) as excinfo:
        sandbox.read_file(root, "swapped.txt")
    assert _error(excinfo) == ExecutionErrorCode.FORBIDDEN_PATH


def test_creating_a_symlink_via_write_is_impossible():
    """09 §3: "Creating symlinks via a tool is disallowed by default." There
    is no operation exposed that creates a symlink — `create_file`/`write_file`
    always open with O_NOFOLLOW and write plain bytes, never `os.symlink`."""

    import inspect

    from server.fs import sandbox as sandbox_module

    source = inspect.getsource(sandbox_module)
    assert "os.symlink" not in source


# ── FS-T5 / FS-T6: cross-user / cross-graph isolation ───────────────────


def test_user_a_cannot_derive_user_bs_root(sandbox: FilesystemSandbox):
    a, b = uuid.uuid4(), uuid.uuid4()
    root_a = sandbox.root_for(user_id=a, graph_id=None, label="notes")
    root_b = sandbox.root_for(user_id=b, graph_id=None, label="notes")

    sandbox.create_file(root_b, "private.txt", "b's secret")

    assert root_a != root_b
    # A's own root cannot be walked into B's — B's root is not even a
    # descendant of A's, so "a's-root/../.../b" is rejected before any
    # filesystem call, and there is no argument to root_for that lets A
    # name B's user_id (it is taken from the authorized request, never from
    # tool arguments — see server/tools/platforms.py, task #6).
    with pytest.raises(ExecutionError):
        sandbox.read_file(root_a, f"../../{b}/private/notes/private.txt")


def test_same_label_different_users_are_different_physical_roots(sandbox: FilesystemSandbox):
    a, b = uuid.uuid4(), uuid.uuid4()
    root_a = sandbox.root_for(user_id=a, graph_id=None, label="shared-label")
    root_b = sandbox.root_for(user_id=b, graph_id=None, label="shared-label")
    assert root_a != root_b
    assert str(a) in root_a
    assert str(b) in root_b


def test_graph_scoped_root_is_distinct_from_any_users_private_root(sandbox: FilesystemSandbox):
    user_id = uuid.uuid4()
    graph_id = uuid.uuid4()
    private_root = sandbox.root_for(user_id=user_id, graph_id=None, label="x")
    graph_root = sandbox.root_for(user_id=user_id, graph_id=graph_id, label="x")
    assert private_root != graph_root
    assert str(graph_id) in graph_root


def test_sandbox_root_scope_label_cannot_be_a_path(sandbox: FilesystemSandbox):
    """The scope value is a label, never a path — see server/fs/__init__.py's
    module docstring for why this is deliberately stricter than treating
    resource_scope['sandbox_root'] as a raw filesystem path."""

    user_id = uuid.uuid4()
    for malicious_label in ("../../etc", "/etc/passwd", "a/b", "..", ".", ""):
        with pytest.raises(ExecutionError):
            sandbox.root_for(user_id=user_id, graph_id=None, label=malicious_label)


# ── FS-T7: sensitive host paths structurally unreachable ────────────────


def test_configuring_base_root_under_a_sensitive_path_fails_at_construction():
    for sensitive in ("/etc/hypermind", "/root/sandboxes"):
        with pytest.raises(ExecutionError):
            FilesystemSandbox(base_root=sensitive)


def test_no_operation_accepts_a_host_path_outside_any_root(user_root):
    sandbox, root, _ = user_root
    for sensitive in ("/root/.ssh/id_rsa", "/etc/shadow", "/proc/self/environ"):
        with pytest.raises(ExecutionError):
            sandbox.read_file(root, sensitive)


# ── FS-T3 / FS-T4: archive extraction (zip-slip / zip-bomb) ─────────────


def test_zip_slip_entry_is_rejected_not_written(user_root, tmp_path):
    sandbox, root, _ = user_root
    archive_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("../../evil_escape.txt", "pwned")
    sandbox.create_file(root, "evil.zip", "")
    shutil.copyfile(archive_path, Path(root) / "evil.zip")

    with pytest.raises(ExecutionError) as excinfo:
        sandbox.extract_zip(root, "evil.zip", "extracted")
    assert _error(excinfo) == ExecutionErrorCode.FORBIDDEN_PATH
    # And critically: nothing escaped onto disk outside the sandbox.
    assert not (Path(root).parent.parent / "evil_escape.txt").exists()


def test_zip_with_absolute_entry_path_is_rejected(user_root, tmp_path):
    sandbox, root, _ = user_root
    archive_path = tmp_path / "abs.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("/etc/evil.txt", "pwned")
    shutil.copyfile(archive_path, Path(root) / "abs.zip")

    with pytest.raises(ExecutionError):
        sandbox.extract_zip(root, "abs.zip", "extracted")


def test_zip_entry_that_is_a_symlink_is_refused(user_root, tmp_path):
    sandbox, root, _ = user_root
    archive_path = tmp_path / "symlink.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        info = zipfile.ZipInfo("link_entry")
        info.external_attr = (stat_module.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    shutil.copyfile(archive_path, Path(root) / "symlink.zip")

    with pytest.raises(ExecutionError) as excinfo:
        sandbox.extract_zip(root, "symlink.zip", "extracted")
    assert _error(excinfo) == ExecutionErrorCode.FORBIDDEN_PATH


def test_zip_bomb_declared_size_is_capped(tmp_path):
    sandbox = FilesystemSandbox(
        base_root=str(tmp_path / "sandboxes"), max_archive_uncompressed_bytes=1000
    )
    user_id = uuid.uuid4()
    root = sandbox.root_for(user_id=user_id, graph_id=None, label="notes")
    archive_path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("huge.txt", "A" * 10_000)
    shutil.copyfile(archive_path, Path(root) / "bomb.zip")

    with pytest.raises(ExecutionError) as excinfo:
        sandbox.extract_zip(root, "bomb.zip", "extracted")
    assert _error(excinfo) == ExecutionErrorCode.RESOURCE_EXHAUSTED


def test_zip_entry_count_is_capped(tmp_path):
    sandbox = FilesystemSandbox(base_root=str(tmp_path / "sandboxes"), max_archive_entries=3)
    user_id = uuid.uuid4()
    root = sandbox.root_for(user_id=user_id, graph_id=None, label="notes")
    archive_path = tmp_path / "many.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        for i in range(10):
            zf.writestr(f"file_{i}.txt", "x")
    shutil.copyfile(archive_path, Path(root) / "many.zip")

    with pytest.raises(ExecutionError) as excinfo:
        sandbox.extract_zip(root, "many.zip", "extracted")
    assert _error(excinfo) == ExecutionErrorCode.RESOURCE_EXHAUSTED


def test_valid_zip_extracts_cleanly(user_root, tmp_path):
    sandbox, root, _ = user_root
    archive_path = tmp_path / "ok.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.writestr("a.txt", "hello")
        zf.writestr("dir/b.txt", "world")
    shutil.copyfile(archive_path, Path(root) / "ok.zip")

    result = sandbox.extract_zip(root, "ok.zip", "extracted")
    assert result.metadata["count"] == 2
    assert sandbox.read_file(root, "extracted/a.txt").content == "hello"
    assert sandbox.read_file(root, "extracted/dir/b.txt").content == "world"


def test_tar_slip_entry_is_rejected(user_root, tmp_path):
    sandbox, root, _ = user_root
    archive_path = tmp_path / "evil.tar"
    with tarfile.open(archive_path, "w") as tf:
        data = b"pwned"
        import io

        info = tarfile.TarInfo(name="../../evil_tar_escape.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    shutil.copyfile(archive_path, Path(root) / "evil.tar")

    with pytest.raises(ExecutionError):
        sandbox.extract_tar(root, "evil.tar", "extracted")
    assert not (Path(root).parent.parent / "evil_tar_escape.txt").exists()


def test_tar_symlink_entry_is_refused(user_root, tmp_path):
    sandbox, root, _ = user_root
    archive_path = tmp_path / "linked.tar"
    with tarfile.open(archive_path, "w") as tf:
        info = tarfile.TarInfo(name="link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    shutil.copyfile(archive_path, Path(root) / "linked.tar")

    with pytest.raises(ExecutionError):
        sandbox.extract_tar(root, "linked.tar", "extracted")


# ── FS-T10: size limits, quota, no world-writable files ─────────────────


def test_oversized_write_fails_explicitly_not_partially(tmp_path):
    sandbox = FilesystemSandbox(base_root=str(tmp_path / "sandboxes"), max_file_bytes=10)
    user_id = uuid.uuid4()
    root = sandbox.root_for(user_id=user_id, graph_id=None, label="notes")

    with pytest.raises(ExecutionError) as excinfo:
        sandbox.create_file(root, "big.txt", "x" * 100)
    assert _error(excinfo) == ExecutionErrorCode.RESOURCE_EXHAUSTED
    assert not (Path(root) / "big.txt").exists()


def test_sandbox_quota_is_enforced(tmp_path):
    sandbox = FilesystemSandbox(
        base_root=str(tmp_path / "sandboxes"), max_file_bytes=1000, max_sandbox_bytes=15
    )
    user_id = uuid.uuid4()
    root = sandbox.root_for(user_id=user_id, graph_id=None, label="notes")

    sandbox.create_file(root, "a.txt", "1234567890")  # 10 bytes, under quota
    with pytest.raises(ExecutionError) as excinfo:
        sandbox.create_file(root, "b.txt", "1234567890")  # would push to 20 > 15
    assert _error(excinfo) == ExecutionErrorCode.RESOURCE_EXHAUSTED


def test_created_files_are_not_world_or_group_writable(user_root):
    sandbox, root, _ = user_root
    sandbox.create_file(root, "perm.txt", "x")
    mode = stat_module.S_IMODE(os.stat(Path(root) / "perm.txt").st_mode)
    assert mode & 0o077 == 0, oct(mode)


def test_sandbox_root_itself_is_not_world_accessible(user_root):
    _, root, _ = user_root
    mode = stat_module.S_IMODE(os.stat(root).st_mode)
    assert mode & 0o077 == 0, oct(mode)


# ── task temp cleanup ─────────────────────────────────────────────────


def test_task_temp_is_cleaned_up(sandbox: FilesystemSandbox):
    task_id = uuid.uuid4()
    temp_root = sandbox.task_temp(task_id=task_id)
    (Path(temp_root) / "scratch.txt").write_text("temp data")
    assert Path(temp_root).exists()

    sandbox.cleanup_task(task_id=task_id)
    assert not Path(temp_root).exists()


def test_cleanup_of_a_task_that_never_touched_fs_is_a_no_op(sandbox: FilesystemSandbox):
    sandbox.cleanup_task(task_id=uuid.uuid4())  # must not raise


# ── operation-shape correctness ─────────────────────────────────────────


def test_write_file_requires_existing_file_create_file_requires_absence(user_root):
    sandbox, root, _ = user_root
    with pytest.raises(ExecutionError):
        sandbox.write_file(root, "missing.txt", "content")  # write needs existing

    sandbox.create_file(root, "new.txt", "v1")
    with pytest.raises(ExecutionError):
        sandbox.create_file(root, "new.txt", "v2")  # create needs absence

    sandbox.write_file(root, "new.txt", "v2")
    assert sandbox.read_file(root, "new.txt").content == "v2"


def test_delete_file_refuses_a_directory(user_root):
    sandbox, root, _ = user_root
    sandbox.create_file(root, "dir/inner.txt", "x")
    with pytest.raises(ExecutionError):
        sandbox.delete_file(root, "dir")


def test_bulk_delete_removes_a_subtree_within_the_sandbox_only(user_root):
    sandbox, root, _ = user_root
    sandbox.create_file(root, "tree/a.txt", "1")
    sandbox.create_file(root, "tree/sub/b.txt", "2")

    result = sandbox.bulk_delete(root, "tree")
    assert result.metadata["removed"] >= 3  # a.txt, sub/, sub/b.txt
    assert not (Path(root) / "tree").exists()


def test_list_directory_never_surfaces_a_symlink_as_traversable(user_root):
    sandbox, root, _ = user_root
    sandbox.create_file(root, "real.txt", "x")
    os.symlink("/etc/passwd", os.path.join(root, "link.txt"))

    result = sandbox.list_directory(root, ".")
    assert "real.txt" in result.content
    assert "link.txt" not in result.content



# ── integration hardening: the quota is per principal, not per label ────


def test_the_storage_quota_covers_every_label_a_principal_names(tmp_path):
    """A `sandbox_root` label is caller-chosen. Charging the quota per label let a
    principal fill `max_sandbox_bytes` again under every new name; it is charged
    against the principal's whole area instead."""


    sandbox = FilesystemSandbox(base_root=str(tmp_path / "sb"), max_sandbox_bytes=1000, max_file_bytes=1000)
    user = uuid.uuid4()
    first = sandbox.root_for(user_id=user, graph_id=None, label="a")
    sandbox.create_file(first, "one.txt", "x" * 700)

    second = sandbox.root_for(user_id=user, graph_id=None, label="b")
    with pytest.raises(ExecutionError) as excinfo:
        sandbox.create_file(second, "two.txt", "y" * 700)
    assert excinfo.value.code == ExecutionErrorCode.RESOURCE_EXHAUSTED

    # Another principal's usage is not charged to this one.
    other = sandbox.root_for(user_id=uuid.uuid4(), graph_id=None, label="a")
    sandbox.create_file(other, "three.txt", "z" * 700)
