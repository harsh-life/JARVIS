"""Desktop release integrity (OD-DESK-3, DESK-8): one tag, one commit, and
nothing unsigned presented as signed.

Three layers:

* ``resolve`` runs against real git repositories (a bare "remote" and a
  clone), covering lightweight and annotated tags, a missing tag, a moved
  tag, a tag off ``main``, a tag on a non-commit, a version mismatch and
  hostile names.
* the signing classifiers are fed the text ``codesign -dvvv`` and
  ``Get-AuthenticodeSignature`` actually print, ad-hoc and linker signatures
  included.
* ``assemble`` refuses builds from different commits, swapped bytes,
  unlisted files and missing platforms, and its notes and title say
  "unsigned" whenever anything is.

The workflow itself is checked statically: what it checks out, how it
calls ``gh``, and where untrusted expressions may appear.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "desktop" / "scripts" / "release_integrity.py"
WORKFLOW = ROOT / ".github" / "workflows" / "desktop-release.yml"

_spec = importlib.util.spec_from_file_location("release_integrity", SCRIPT)
ri = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules.setdefault("release_integrity", ri)  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(ri)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


# ── a throwaway remote + clone ────────────────────────────────────────────


def _g(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_versions(repo: Path, version: str) -> None:
    (repo / "desktop" / "src-tauri").mkdir(parents=True, exist_ok=True)
    (repo / "desktop" / "src-tauri" / "tauri.conf.json").write_text(json.dumps({"version": version}))
    (repo / "desktop" / "package.json").write_text(json.dumps({"name": "x", "version": version}))
    (repo / "desktop" / "src-tauri" / "Cargo.toml").write_text(
        f'[package]\nname = "x"\nversion = "{version}"\n\n[dependencies]\nfoo = {{ version = "9.9.9" }}\n'
    )


def _commit(repo: Path, msg: str) -> str:
    _g(repo, "add", "-A")
    _g(repo, "commit", "-q", "--allow-empty", "-m", msg)
    return _g(repo, "rev-parse", "HEAD")


class Repo:
    def __init__(self, tmp: Path):
        self.work = tmp / "work"
        self.remote = tmp / "remote.git"
        self.clone = tmp / "clone"
        self.work.mkdir()
        _g(self.work, "init", "-q", "-b", "main")
        _write_versions(self.work, "0.1.0")
        self.main_sha = _commit(self.work, "release 0.1.0")
        subprocess.run(["git", "clone", "-q", "--bare", str(self.work), str(self.remote)], check=True)
        _g(self.work, "remote", "add", "origin", str(self.remote))
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.clone)], check=True)

    def push(self, *refs: str) -> None:
        _g(self.work, "push", "-q", "--force", "origin", *refs)

    def resolve(self, tag: str, *, event: str = "workflow_dispatch", event_sha: str | None = None, **kw):
        ref = f"refs/tags/{tag}" if event == "push" else "refs/heads/main"
        return ri.resolve(
            self.clone,
            event=event,
            ref=ref,
            input_tag="" if event == "push" else tag,
            event_sha=event_sha or self.main_sha,
            **kw,
        )


@pytest.fixture
def repo(tmp_path) -> Repo:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    return Repo(tmp_path)


# ── resolve ───────────────────────────────────────────────────────────────


@needs_git
def test_lightweight_tag_resolves_to_its_commit(repo):
    _g(repo.work, "tag", "desktop-v0.1.0")
    repo.push("refs/tags/desktop-v0.1.0")
    res = repo.resolve("desktop-v0.1.0")
    assert (res.tag, res.sha, res.version) == ("desktop-v0.1.0", repo.main_sha, "0.1.0")


@needs_git
def test_annotated_tag_is_peeled_to_the_commit_not_the_tag_object(repo):
    _g(repo.work, "tag", "-a", "desktop-v0.1.0", "-m", "release")
    repo.push("refs/tags/desktop-v0.1.0")
    tag_object = _g(repo.work, "rev-parse", "desktop-v0.1.0")
    assert tag_object != repo.main_sha
    assert repo.resolve("desktop-v0.1.0").sha == repo.main_sha


@needs_git
def test_dispatch_builds_the_tag_commit_not_the_dispatching_branch_head(repo):
    _g(repo.work, "tag", "desktop-v0.1.0")
    repo.push("refs/tags/desktop-v0.1.0")
    # main moves on after the tag; a manual run started from main must still
    # resolve to the tagged commit.
    _write_versions(repo.work, "0.1.0")
    (repo.work / "later.txt").write_text("later")
    later = _commit(repo.work, "later work")
    repo.push("main")
    res = repo.resolve("desktop-v0.1.0", event_sha=later)
    assert res.sha == repo.main_sha != later


@needs_git
def test_a_tag_that_only_exists_locally_or_not_at_all_is_refused(repo):
    _g(repo.clone, "tag", "desktop-v0.1.0")  # local only, never pushed
    with pytest.raises(ri.IntegrityError, match="does not exist on origin"):
        repo.resolve("desktop-v0.1.0")
    with pytest.raises(ri.IntegrityError, match="does not exist on origin"):
        repo.resolve("desktop-v9.9.9")


@needs_git
def test_push_run_fails_when_the_tag_moved_after_the_push(repo):
    _g(repo.work, "tag", "desktop-v0.1.0")
    repo.push("refs/tags/desktop-v0.1.0")
    pushed = repo.main_sha
    (repo.work / "x").write_text("x")
    moved_to = _commit(repo.work, "other")
    repo.push("main")
    _g(repo.work, "tag", "-f", "desktop-v0.1.0", moved_to)
    repo.push("refs/tags/desktop-v0.1.0")
    with pytest.raises(ri.IntegrityError, match="moved after it was pushed"):
        repo.resolve("desktop-v0.1.0", event="push", event_sha=pushed)
    assert repo.resolve("desktop-v0.1.0", event="push", event_sha=moved_to).sha == moved_to


@needs_git
def test_expect_sha_catches_a_tag_moved_during_the_run(repo):
    _g(repo.work, "tag", "desktop-v0.1.0")
    repo.push("refs/tags/desktop-v0.1.0")
    built = repo.resolve("desktop-v0.1.0").sha
    (repo.work / "x").write_text("x")
    other = _commit(repo.work, "other")
    repo.push("main")
    _g(repo.work, "tag", "-f", "desktop-v0.1.0", other)
    repo.push("refs/tags/desktop-v0.1.0")
    # The clone still has the old tag locally; the remote is what counts.
    with pytest.raises(ri.IntegrityError, match="moved during the run"):
        repo.resolve("desktop-v0.1.0", expect_sha=built)


@needs_git
def test_tag_on_a_commit_not_on_main_is_refused(repo):
    _g(repo.work, "checkout", "-q", "-b", "side")
    (repo.work / "side.txt").write_text("unreviewed")
    side = _commit(repo.work, "unreviewed")
    _g(repo.work, "tag", "desktop-v0.1.0", side)
    repo.push("side", "refs/tags/desktop-v0.1.0")
    with pytest.raises(ri.IntegrityError, match="not on main"):
        repo.resolve("desktop-v0.1.0")


@needs_git
def test_tag_on_a_tree_is_refused(repo):
    tree = _g(repo.work, "rev-parse", "HEAD^{tree}")
    _g(repo.work, "tag", "desktop-v0.1.0", tree)
    repo.push("refs/tags/desktop-v0.1.0")
    with pytest.raises(ri.IntegrityError, match="points at a tree"):
        repo.resolve("desktop-v0.1.0")


@needs_git
def test_tag_version_must_match_every_declared_version(repo):
    _g(repo.work, "tag", "desktop-v0.2.0")
    repo.push("refs/tags/desktop-v0.2.0")
    with pytest.raises(ri.IntegrityError, match="names version 0.2.0") as exc:
        repo.resolve("desktop-v0.2.0")
    for path in ("tauri.conf.json", "package.json", "Cargo.toml"):
        assert path in str(exc.value)


@needs_git
def test_a_prerelease_tag_matches_its_core_version(repo):
    _g(repo.work, "tag", "desktop-v0.1.0-rc.1")
    repo.push("refs/tags/desktop-v0.1.0-rc.1")
    assert repo.resolve("desktop-v0.1.0-rc.1").version == "0.1.0"


@pytest.mark.parametrize(
    "tag",
    [
        "",
        "v0.1.0",
        "desktop-v0.1",
        "desktop-v01.0.0",
        "desktop-v0.1.0-",
        "desktop-v0.1.0+build",
        "desktop-v0.1.0;id",
        "desktop-v0.1.0$(id)",
        "desktop-v0.1.0-rc`id`",
        "desktop-v0.1.0 ",
        "refs/tags/desktop-v0.1.0",
    ],
)
def test_malformed_or_hostile_tag_names_are_refused(tag):
    with pytest.raises(ri.IntegrityError, match="not a desktop release tag"):
        ri.parse_tag(tag)


def test_the_tag_comes_from_the_trigger():
    assert ri.tag_from_event("push", "refs/tags/desktop-v1.2.3", "") == "desktop-v1.2.3"
    assert ri.tag_from_event("workflow_dispatch", "refs/heads/main", "desktop-v1.2.3") == "desktop-v1.2.3"
    with pytest.raises(ri.IntegrityError):
        ri.tag_from_event("push", "refs/heads/main", "")
    with pytest.raises(ri.IntegrityError):
        ri.tag_from_event("push", "refs/tags/desktop-v1.2.3", "desktop-v9.9.9")
    with pytest.raises(ri.IntegrityError):
        ri.tag_from_event("workflow_dispatch", "refs/heads/main", "")
    with pytest.raises(ri.IntegrityError):
        ri.tag_from_event("pull_request", "refs/pull/1/merge", "desktop-v1.2.3")


@needs_git
def test_the_real_tree_declares_one_consistent_version():
    versions = ri.declared_versions(ROOT, subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip())
    assert len(set(versions.values())) == 1, versions


# ── signing classification ────────────────────────────────────────────────

ADHOC = """Executable=/Volumes/JARVIS Companion/JARVIS Companion.app/Contents/MacOS/jarvis-companion
Identifier=jarvis_companion-55554944
Format=app bundle with Mach-O universal (x86_64 arm64)
CodeDirectory v=20400 size=12345 flags=0x20002(adhoc,linker-signed) hashes=380+0 location=embedded
Signature=adhoc
Info.plist=not bound
TeamIdentifier=not set
Sealed Resources=none
"""

DEVELOPER_ID = """Executable=/Volumes/JARVIS Companion/JARVIS Companion.app/Contents/MacOS/jarvis-companion
Identifier=com.hypermind.jarvis.companion
Format=app bundle with Mach-O universal (x86_64 arm64)
CodeDirectory v=20500 size=12345 flags=0x10000(runtime) hashes=380+7 location=embedded
Signature size=9000
Authority=Developer ID Application: Example Org (ABCDE12345)
Authority=Developer ID Certification Authority
Authority=Apple Root CA
Timestamp=11 Nov 2026 at 10:00:00
TeamIdentifier=ABCDE12345
Sealed Resources version=2 rules=13 files=20
"""

APPLE_DEVELOPMENT = DEVELOPER_ID.replace("Developer ID Application", "Apple Development")
NOT_SIGNED = "/Volumes/JARVIS Companion/JARVIS Companion.app: code object is not signed at all\n"


def test_an_adhoc_or_linker_signature_is_not_a_signature_even_when_it_verifies():
    s = ri.classify_macos(0, ADHOC)
    assert (s.signed, s.status) == (False, "ad-hoc")


def test_only_a_verifying_developer_id_signature_counts_as_signed():
    assert ri.classify_macos(0, DEVELOPER_ID).signed is True
    assert ri.classify_macos(0, DEVELOPER_ID).status == "developer-id"
    assert (ri.classify_macos(1, DEVELOPER_ID).signed, ri.classify_macos(1, DEVELOPER_ID).status) == (False, "invalid")
    assert ri.classify_macos(0, APPLE_DEVELOPMENT).signed is False
    assert ri.classify_macos(1, NOT_SIGNED).status == "unsigned"
    assert ri.classify_macos(0, "").signed is False


@pytest.mark.parametrize("status", ["NotSigned", "UnknownError", "HashMismatch", "NotTrusted", ""])
def test_authenticode_counts_only_valid(status):
    assert ri.classify_windows(status, "CN=Example Org").signed is False
    assert ri.classify_windows("Valid", "CN=Example Org").signed is True
    assert ri.classify_windows("Valid", None).signed is False


# ── inspect + assemble ────────────────────────────────────────────────────

SHA = "a" * 40
OTHER_SHA = "b" * 40
TAG = "desktop-v0.1.0"
FILES = {
    "Linux": ["jarvis-companion_0.1.0_amd64.deb", "JARVIS Companion_0.1.0_amd64.AppImage"],
    "macOS": ["JARVIS Companion_0.1.0_universal.dmg"],
    "Windows": ["JARVIS Companion_0.1.0_x64_en-US.msi"],
}


def _fake_head(monkeypatch, sha=SHA):
    real = ri._git

    def fake(repo, *args, check=True):
        if args == ("rev-parse", "HEAD"):
            return subprocess.CompletedProcess(args, 0, sha + "\n", "")
        return real(repo, *args, check=check)

    monkeypatch.setattr(ri, "_git", fake)


def _build(tmp: Path, monkeypatch, *, signed: dict[str, bool] | None = None, sha=SHA, tag=TAG) -> Path:
    """Simulate three build jobs' upload directories merged into one dist/."""

    signed = signed or {}
    dist = tmp / "dist"
    dist.mkdir(exist_ok=True)
    _fake_head(monkeypatch, sha)
    for os_name, names in FILES.items():
        out = tmp / f"out-{os_name}"
        out.mkdir()
        for n in names:
            (out / n).write_bytes(f"{os_name}:{n}".encode())
        sig = ri.Signing(True, "developer-id", "Developer ID: x") if signed.get(os_name) else ri.Signing(
            False, "ad-hoc", "ad-hoc"
        )
        check = (lambda _p: (ri.LINUX_SIGNING, {})) if os_name == "Linux" else (lambda _p, s=sig: (s, {}))
        ri.inspect(source=tmp, out_dir=out, os_name=os_name, tag=tag, sha=sha, expect_signed=False, platform_check=check)
        for p in out.iterdir():
            shutil.copy(p, dist / p.name)
    return dist


def test_unsigned_release_says_unsigned_in_title_and_notes(tmp_path, monkeypatch):
    dist = _build(tmp_path, monkeypatch)
    asm = ri.assemble(dist=dist, tag=TAG, sha=SHA)
    assert asm.title == "JARVIS Companion 0.1.0 (unsigned)"
    assert "**Unsigned build.**" in asm.notes
    assert not asm.any_signed
    assert ri.GATE_STATEMENT in asm.notes and ri.BOUNDARY_STATEMENT in asm.notes
    assert SHA in asm.notes
    report = (dist / "SIGNING.txt").read_text()
    assert "signed=yes" not in report
    assert report.count("signed=no") == 4
    assert set(asm.assets) == {*sum(FILES.values(), []), "SHA256SUMS.txt", "SIGNING.txt"}
    sums = (dist / "SHA256SUMS.txt").read_text().splitlines()
    assert len(sums) == 4 and all(re.match(r"^[0-9a-f]{64}  \S", line) for line in sums)


def test_partially_signed_release_is_labelled_partial(tmp_path, monkeypatch):
    dist = _build(tmp_path, monkeypatch, signed={"macOS": True})
    asm = ri.assemble(dist=dist, tag=TAG, sha=SHA)
    assert asm.title.endswith("(partially signed)")
    assert "Partially signed" in asm.notes and "**unsigned**" in asm.notes


def test_fully_signed_release_still_calls_linux_unsigned(tmp_path, monkeypatch):
    dist = _build(tmp_path, monkeypatch, signed={"macOS": True, "Windows": True})
    asm = ri.assemble(dist=dist, tag=TAG, sha=SHA)
    assert asm.title == "JARVIS Companion 0.1.0"
    assert "Linux packages are unsigned" in asm.notes
    assert ri.GATE_STATEMENT in asm.notes


def test_builds_from_different_commits_are_refused(tmp_path, monkeypatch):
    dist = _build(tmp_path, monkeypatch)
    rec = json.loads((dist / "BUILD-Windows.json").read_text())
    rec["commit"] = OTHER_SHA
    (dist / "BUILD-Windows.json").write_text(json.dumps(rec))
    with pytest.raises(ri.IntegrityError, match="built from b+, not a+"):
        ri.assemble(dist=dist, tag=TAG, sha=SHA)


def test_the_release_sha_must_match_the_builds(tmp_path, monkeypatch):
    dist = _build(tmp_path, monkeypatch)
    with pytest.raises(ri.IntegrityError, match="not b+"):
        ri.assemble(dist=dist, tag=TAG, sha=OTHER_SHA)
    with pytest.raises(ri.IntegrityError, match="not desktop-v0.2.0"):
        ri.assemble(dist=dist, tag="desktop-v0.2.0", sha=SHA)


def test_swapped_bytes_are_refused(tmp_path, monkeypatch):
    dist = _build(tmp_path, monkeypatch)
    (dist / FILES["Windows"][0]).write_bytes(b"something else")
    with pytest.raises(ri.IntegrityError, match="SHA-256 differs"):
        ri.assemble(dist=dist, tag=TAG, sha=SHA)


def test_unlisted_and_missing_files_are_refused(tmp_path, monkeypatch):
    dist = _build(tmp_path, monkeypatch)
    (dist / "extra.msi").write_bytes(b"x")
    with pytest.raises(ri.IntegrityError, match="no build recorded"):
        ri.assemble(dist=dist, tag=TAG, sha=SHA)
    (dist / "extra.msi").unlink()
    (dist / FILES["Linux"][1]).unlink()
    with pytest.raises(ri.IntegrityError, match="was not downloaded"):
        ri.assemble(dist=dist, tag=TAG, sha=SHA)


def test_a_missing_platform_is_refused(tmp_path, monkeypatch):
    dist = _build(tmp_path, monkeypatch)
    (dist / "BUILD-macOS.json").unlink()
    with pytest.raises(ri.IntegrityError, match="a release needs"):
        ri.assemble(dist=dist, tag=TAG, sha=SHA)


def test_a_record_cannot_claim_a_signed_linux_package(tmp_path, monkeypatch):
    dist = _build(tmp_path, monkeypatch)
    rec = json.loads((dist / "BUILD-Linux.json").read_text())
    rec["artifacts"][0]["signing"]["signed"] = True
    (dist / "BUILD-Linux.json").write_text(json.dumps(rec))
    with pytest.raises(ri.IntegrityError, match="never signed"):
        ri.assemble(dist=dist, tag=TAG, sha=SHA)


def test_inspect_refuses_a_checkout_that_is_not_the_release_commit(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    (out / FILES["Windows"][0]).write_bytes(b"x")
    _fake_head(monkeypatch, OTHER_SHA)
    with pytest.raises(ri.IntegrityError, match="built tree is b+"):
        ri.inspect(source=tmp_path, out_dir=out, os_name="Windows", tag=TAG, sha=SHA, expect_signed=False,
                   platform_check=lambda _p: (ri.Signing(False, "unsigned", ""), {}))


def test_inspect_fails_when_secrets_exist_but_nothing_verifies(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    (out / FILES["macOS"][0]).write_bytes(b"x")
    _fake_head(monkeypatch)
    adhoc = lambda _p: (ri.classify_macos(0, ADHOC), {})  # noqa: E731
    with pytest.raises(ri.IntegrityError, match="no verified signature"):
        ri.inspect(source=tmp_path, out_dir=out, os_name="macOS", tag=TAG, sha=SHA, expect_signed=True,
                   platform_check=adhoc)
    assert not (out / "BUILD-macOS.json").exists()


@pytest.mark.parametrize("name", ["-rf.msi", ".hidden.msi", "a\nb.msi", "a;b.msi", "notes.txt"])
def test_inspect_refuses_unexpected_artifact_names(tmp_path, monkeypatch, name):
    out = tmp_path / "out"
    out.mkdir()
    (out / FILES["Windows"][0]).write_bytes(b"x")
    try:
        (out / name).write_bytes(b"x")
    except OSError:
        pytest.skip("this filesystem cannot hold that name")
    _fake_head(monkeypatch)
    with pytest.raises(ri.IntegrityError):
        ri.inspect(source=tmp_path, out_dir=out, os_name="Windows", tag=TAG, sha=SHA, expect_signed=False,
                   platform_check=lambda _p: (ri.Signing(False, "unsigned", ""), {}))


# ── the workflow, statically ──────────────────────────────────────────────


@pytest.fixture(scope="module")
def wf() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text())
    data["on"] = data.pop(True, data.get("on"))  # YAML 1.1 reads `on:` as True
    return data


def _steps(wf, job):
    return wf["jobs"][job]["steps"]


def _uses(step, action):
    return str(step.get("uses", "")).startswith(action + "@")


def test_triggers_are_tag_push_and_manual_with_a_tag_only(wf):
    assert set(wf["on"]) == {"push", "workflow_dispatch"}
    assert set(wf["on"]["push"]) == {"tags"} and wf["on"]["push"]["tags"] == ["desktop-v*"]
    assert set(wf["on"]["workflow_dispatch"]["inputs"]) == {"tag"}


def test_only_the_draft_job_can_write_and_nothing_else_is_granted(wf):
    assert wf["permissions"] == {"contents": "read"}
    for name, job in wf["jobs"].items():
        if name == "draft-release":
            assert job["permissions"] == {"contents": "write"}
        else:
            assert "permissions" not in job, name


def test_builds_check_out_the_resolved_commit_without_a_persisted_token(wf):
    build = wf["jobs"]["build"]
    assert "resolve" in (build["needs"] if isinstance(build["needs"], list) else [build["needs"]])
    checkouts = [s for s in build["steps"] if _uses(s, "actions/checkout")]
    product = [s for s in checkouts if "path" not in s.get("with", {})]
    assert len(product) == 1
    assert product[0]["with"]["ref"] == "${{ needs.resolve.outputs.sha }}"
    for s in checkouts:
        assert s["with"].get("persist-credentials") is False
    runs = "\n".join(s.get("run", "") for s in build["steps"])
    assert '[ "$HEAD_SHA" = "$RELEASE_SHA" ]' in runs
    assert "release_integrity.py inspect" in runs


def test_the_draft_never_creates_a_tag_and_rechecks_it(wf):
    runs = [s.get("run", "") for s in _steps(wf, "draft-release")]
    creates = [r for r in runs if "gh release create" in r]
    assert len(creates) == 1
    assert "--verify-tag" in creates[0] and "--draft" in creates[0] and "--prerelease" in creates[0]
    assert "--target" not in creates[0]
    assert '"${ASSETS[@]}"' in creates[0]  # names with spaces stay one argument
    idx = runs.index(creates[0])
    assert any("--expect-sha" in r for r in runs[:idx]), "tag must be re-checked before the draft"
    assert any("--expect-sha" in r for r in runs[idx + 1 :]), "tag must be re-checked after the draft"
    assert any("release_integrity.py assemble" in r for r in runs[:idx])
    assert not any(re.search(r"gh release (edit|upload)|--draft=false|git (tag|push)", r) for r in runs)


def test_no_untrusted_expression_is_interpolated_into_a_script(wf):
    untrusted = re.compile(r"\$\{\{\s*(github\.(ref_name|ref|head_ref|event)|inputs\.|matrix\.)")
    for name, job in wf["jobs"].items():
        for step in job["steps"]:
            assert not untrusted.search(step.get("run", "")), f"{name}: {step.get('name')}"


def test_release_builds_use_no_cache(wf):
    for job in wf["jobs"].values():
        for step in job["steps"]:
            assert not _uses(step, "actions/cache"), step
            assert "cache" not in step.get("with", {}), step


def test_the_release_notes_keep_the_gate_and_the_boundary():
    # The gate text in the notes is the project's own (README, CLAUDE.md).
    assert "Real user data is not cleared" in ri.GATE_STATEMENT
    assert "never approves, confirms or runs anything" in ri.BOUNDARY_STATEMENT
    status = (ROOT / "docs" / "PROJECT_STATUS.md").read_text()
    assert "Before putting real data anywhere near this" in status
