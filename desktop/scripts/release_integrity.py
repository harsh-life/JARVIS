#!/usr/bin/env python3
"""Release integrity for the desktop companion (.github/workflows/desktop-release.yml).

Three subcommands, one per stage of the release workflow. Standard library
only, so it runs on every runner without installing anything.

  resolve   Which commit is this release? The tag must already exist on the
            remote, have a valid ``desktop-vX.Y.Z[-pre]`` name, peel to a
            commit, be reachable from ``main`` and name the version the app
            declares. On a tag push it must still point at the pushed commit.
            A manual run must have been started from ``refs/heads/main`` and
            execute a commit on main; any other ref is refused.
            Re-run with ``--expect-sha`` it proves the tag has not moved.

  inspect   What did this runner build? It runs in the build job after
            packaging: it checks that the checkout is the resolved commit,
            hashes every artifact and verifies its signature with the
            platform's own tool. A signature is reported only when the
            platform tool verifies a real signing identity. An ad-hoc or
            linker signature counts as unsigned. With ``--expect-signed`` the
            job fails if no verified signature is found, so a broken signing
            secret cannot turn into a quietly unsigned release.

  assemble  Do the three builds belong together? It runs in the release job
            before anything is uploaded. Every record must name the same tag
            and commit, every artifact on disk must match its recorded hash,
            and no file may be unlisted. Then it writes the checksums, the
            signing report, the notes and the title. The notes and title say
            "unsigned" whenever something is unsigned.

Every failure raises ``IntegrityError``. The CLI prints the message and exits 1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

RECORD_SCHEMA = 1
TAG_PREFIX = "desktop-v"
# SemVer core with an optional dot-separated pre-release. Deliberately
# narrower than git's ref-name rules: nothing a shell could interpret.
TAG_RE = re.compile(
    r"^desktop-v(?P<core>(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5}))"
    r"(?:-(?P<pre>[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+){0,7}))?$"
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# Tauri's bundle names contain spaces ("JARVIS Companion_0.1.0_x64_en-US.msi").
# Anything else (a path separator, a control character, a leading dash or dot)
# is refused, because each name ends up as an argument to `gh`.
ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,199}$")

# The versions the tagged tree must declare (the tag's SemVer core).
VERSION_FILES = (
    ("desktop/src-tauri/tauri.conf.json", "json"),
    ("desktop/package.json", "json"),
    ("desktop/src-tauri/Cargo.toml", "cargo"),
)

# What each runner must produce. The release needs all three.
EXPECTED_KINDS: dict[str, tuple[str, ...]] = {
    "Linux": (".deb", ".AppImage"),
    "macOS": (".dmg",),
    "Windows": (".msi",),
}

GATE_STATEMENT = "Real user data is not cleared for this codebase yet (docs/PROJECT_STATUS.md)."
BOUNDARY_STATEMENT = "The companion presents JARVIS's activity; it never approves, confirms or runs anything."


class IntegrityError(Exception):
    """A release-integrity check failed. The message says which one and why."""


# ── git ───────────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and proc.returncode != 0:
        raise IntegrityError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def parse_tag(tag: str) -> str:
    """Validate a release tag name and return its SemVer core ("0.1.0")."""

    match = TAG_RE.match(tag or "")
    if not match:
        raise IntegrityError(
            f"{tag!r} is not a desktop release tag: expected {TAG_PREFIX}MAJOR.MINOR.PATCH "
            "with an optional -prerelease (e.g. desktop-v0.1.0, desktop-v0.2.0-rc.1)"
        )
    return match.group("core")


def tag_from_event(event: str, ref: str, input_tag: str) -> str:
    """The tag the run is about, derived from the trigger and never from a branch."""

    if event == "push":
        if not ref.startswith("refs/tags/"):
            raise IntegrityError(f"a push run must be for a tag ref, got {ref!r}")
        if input_tag:
            raise IntegrityError("a push run takes its tag from the pushed ref, not an input")
        return ref[len("refs/tags/") :]
    if event == "workflow_dispatch":
        if not input_tag:
            raise IntegrityError("a manual run needs the tag input")
        return input_tag
    raise IntegrityError(f"unsupported trigger {event!r}")


def require_dispatch_from_main(event: str, ref: str, main_branch: str) -> None:
    """A manual run is accepted only when it was started from `main` itself.

    GitHub lets anyone with write access dispatch a workflow from any branch
    or tag, and the run then executes that ref's copy of the workflow and of
    this script. Refusing here keeps a manual run from ever resolving a
    release on behalf of unreviewed code. Tag pushes are not affected: their
    ref is the tag, and `resolve` separately requires the tagged commit to be
    on main. The comparison is exact, so ``refs/heads/main-evil``,
    ``refs/heads/feature/main`` and ``refs/tags/main`` are all refused.
    """

    if event == "workflow_dispatch" and ref != f"refs/heads/{main_branch}":
        raise IntegrityError(
            f"a manual run must be started from refs/heads/{main_branch}, not {ref!r}: "
            "release tooling runs only from reviewed main code"
        )


def remote_tag_commit(repo: Path, remote: str, tag: str) -> str:
    """The commit `tag` peels to on `remote`, verified against a fresh fetch.

    The remote is the source of truth: a tag that exists only in the runner's
    clone is not a release tag. Annotated tags are peeled; a tag that does not
    end at a commit (a tree, a blob) is refused.
    """

    ref = f"refs/tags/{tag}"
    out = _git(repo, "ls-remote", remote, ref, f"{ref}^{{}}").stdout
    entries: dict[str, list[str]] = {}
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        entries.setdefault(name, []).append(sha)
    direct = entries.get(ref, [])
    peeled = entries.get(f"{ref}^{{}}", [])
    if not direct:
        raise IntegrityError(
            f"tag {tag} does not exist on {remote}. Create and push it first; "
            "the release workflow never creates tags"
        )
    if len(direct) != 1 or len(peeled) > 1:
        raise IntegrityError(f"{remote} reports {ref} ambiguously: {out.strip()!r}")
    candidate = peeled[0] if peeled else direct[0]

    _git(repo, "fetch", "--no-tags", "--force", remote, f"+{ref}:{ref}")
    obj_type = _git(repo, "cat-file", "-t", candidate).stdout.strip()
    if obj_type != "commit":
        raise IntegrityError(f"tag {tag} points at a {obj_type}, not a commit")
    local = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()
    if local != candidate:
        raise IntegrityError(f"tag {tag}: fetched {local}, but {remote} advertises {candidate}")
    return candidate


def declared_versions(repo: Path, sha: str) -> dict[str, str]:
    """The version each VERSION_FILES entry declares at `sha`."""

    found: dict[str, str] = {}
    for path, kind in VERSION_FILES:
        text = _git(repo, "show", f"{sha}:{path}").stdout
        if kind == "json":
            version = json.loads(text).get("version")
        else:
            package = re.search(r"(?ms)^\[package\]\s*$(.*?)(?=^\[|\Z)", text)
            m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', package.group(1)) if package else None
            version = m.group(1) if m else None
        if not isinstance(version, str):
            raise IntegrityError(f"{path} at {sha[:12]} declares no version")
        found[path] = version
    return found


@dataclass
class Resolution:
    tag: str
    sha: str
    version: str


def resolve(
    repo: Path,
    *,
    event: str,
    ref: str,
    input_tag: str,
    event_sha: str,
    remote: str = "origin",
    main_branch: str = "main",
    expect_sha: str | None = None,
) -> Resolution:
    require_dispatch_from_main(event, ref, main_branch)
    tag = tag_from_event(event, ref, input_tag)
    core = parse_tag(tag)
    sha = remote_tag_commit(repo, remote, tag)

    if event == "push" and event_sha != sha:
        raise IntegrityError(
            f"tag {tag} now points at {sha}, but this run was triggered for {event_sha}: "
            "the tag moved after it was pushed"
        )
    if expect_sha is not None and sha != expect_sha:
        raise IntegrityError(
            f"tag {tag} now points at {sha}, but the artifacts were built from {expect_sha}: "
            "the tag moved during the run"
        )

    main_ref = f"refs/remotes/{remote}/{main_branch}"
    _git(repo, "fetch", "--no-tags", "--force", remote, f"+refs/heads/{main_branch}:{main_ref}")
    if (
        event == "workflow_dispatch"
        and _git(repo, "merge-base", "--is-ancestor", event_sha, main_ref, check=False).returncode != 0
    ):
        # The ref said main, but the code this run executes is not main's history
        # (a rewritten branch, or a mismatched event).
        raise IntegrityError(
            f"this manual run executes {event_sha}, which is not on {main_branch}: "
            "release tooling runs only from reviewed main code"
        )
    if _git(repo, "merge-base", "--is-ancestor", sha, main_ref, check=False).returncode != 0:
        raise IntegrityError(
            f"tag {tag} points at {sha}, which is not on {main_branch}: "
            "releases are cut only from reviewed, merged code"
        )

    mismatched = {p: v for p, v in declared_versions(repo, sha).items() if v != core}
    if mismatched:
        raise IntegrityError(
            f"tag {tag} names version {core}, but at {sha[:12]} "
            + ", ".join(f"{p} declares {v}" for p, v in mismatched.items())
        )
    return Resolution(tag=tag, sha=sha, version=core)


# ── signing classification (pure: the tests feed it real tool output) ─────


@dataclass
class Signing:
    signed: bool  # True only for a verified signature from a real identity
    status: str  # machine-readable: developer-id | authenticode-valid | ad-hoc | unsigned | invalid | ...
    detail: str  # what the platform tool reported, for the human-readable report


def classify_macos(verify_rc: int, display: str) -> Signing:
    """Classify a .app from `codesign --verify --deep --strict` and `codesign -dvvv`.

    `--verify` alone is not enough. On Apple Silicon the linker ad-hoc signs
    every binary, and an ad-hoc signature can verify. A build is signed only if
    it verifies **and** carries a Developer ID Application authority and a team
    identifier.
    """

    if "not signed at all" in display:
        return Signing(False, "unsigned", "no code signature")
    authorities = re.findall(r"(?m)^Authority=(.+)$", display)
    team = re.search(r"(?m)^TeamIdentifier=(.+)$", display)
    team_id = team.group(1).strip() if team else ""
    adhoc = bool(re.search(r"(?m)^Signature=adhoc\s*$", display)) or "linker-signed" in display
    if adhoc or not authorities:
        return Signing(False, "ad-hoc", "ad-hoc signature only (no signing identity). This is not a signature")
    if verify_rc != 0:
        return Signing(False, "invalid", f"signature by {authorities[0]} does not verify")
    if not authorities[0].startswith("Developer ID Application:") or team_id in ("", "not set"):
        return Signing(False, "not-developer-id", f"signed by {authorities[0]}, which is not a Developer ID identity")
    return Signing(True, "developer-id", f"Developer ID: {authorities[0]} (team {team_id})")


def classify_windows(status: str, subject: str | None) -> Signing:
    """Classify an MSI from `Get-AuthenticodeSignature`.

    Only `Valid`, with a signer certificate, counts as signed. A file with an
    untrusted, mismatched or unknown signature is reported as such.
    """

    if status == "Valid" and subject:
        return Signing(True, "authenticode-valid", f"Authenticode: {subject}")
    if status == "NotSigned":
        return Signing(False, "unsigned", "no Authenticode signature")
    return Signing(False, f"authenticode-{status or 'unknown'}".lower(), f"Authenticode status {status or 'unknown'}")


LINUX_SIGNING = Signing(False, "unsigned", "Linux packages are not signed; verify them against SHA256SUMS.txt")


# ── inspect (build jobs) ──────────────────────────────────────────────────


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(*argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, env=env)


def inspect_macos_dmg(dmg: Path) -> tuple[Signing, dict[str, str]]:
    attach = _run("hdiutil", "attach", "-nobrowse", "-readonly", "-plist", str(dmg))
    if attach.returncode != 0:
        raise IntegrityError(f"cannot mount {dmg.name}: {attach.stderr.strip()}")
    mounts = [
        e["mount-point"]
        for e in plistlib.loads(attach.stdout.encode()).get("system-entities", [])
        if e.get("mount-point")
    ]
    if len(mounts) != 1:
        raise IntegrityError(f"{dmg.name}: expected one mount point, got {mounts}")
    mount = Path(mounts[0])
    try:
        apps = sorted(mount.glob("*.app"))
        if len(apps) != 1:
            raise IntegrityError(f"{dmg.name}: expected one .app, found {[a.name for a in apps]}")
        app = str(apps[0])
        verify = _run("codesign", "--verify", "--deep", "--strict", app)
        display = _run("codesign", "-dvvv", app)
        signing = classify_macos(verify.returncode, display.stderr + display.stdout)
        facts = {
            "gatekeeper": "accepted" if _run("spctl", "-a", "-t", "exec", app).returncode == 0 else "rejected",
            "app-notarization-stapled": "yes" if _run("xcrun", "stapler", "validate", app).returncode == 0 else "no",
            "dmg-notarization-stapled": "yes"
            if _run("xcrun", "stapler", "validate", str(dmg)).returncode == 0
            else "no",
        }
    finally:
        _run("hdiutil", "detach", str(mount), "-quiet")
    return signing, facts


def inspect_windows_msi(msi: Path) -> tuple[Signing, dict[str, str]]:
    # The path travels in the environment, never inside the script text.
    script = (
        "$s = Get-AuthenticodeSignature -LiteralPath $env:JARVIS_ARTIFACT; "
        "@{ status = [string]$s.Status; subject = $s.SignerCertificate.Subject; "
        "thumbprint = $s.SignerCertificate.Thumbprint } | ConvertTo-Json -Compress"
    )
    proc = _run("pwsh", "-NoProfile", "-NonInteractive", "-Command", script, env={**os.environ, "JARVIS_ARTIFACT": str(msi)})
    if proc.returncode != 0:
        raise IntegrityError(f"Get-AuthenticodeSignature failed for {msi.name}: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    signing = classify_windows(data.get("status") or "", data.get("subject"))
    facts = {"authenticode-status": data.get("status") or "unknown"}
    if signing.signed and data.get("thumbprint"):
        facts["thumbprint"] = data["thumbprint"]
    return signing, facts


@dataclass
class Artifact:
    file: str
    sha256: str
    size: int
    signing: Signing
    facts: dict[str, str] = field(default_factory=dict)


def collect_artifacts(out_dir: Path, os_name: str) -> list[Path]:
    kinds = EXPECTED_KINDS.get(os_name)
    if kinds is None:
        raise IntegrityError(f"unknown runner OS {os_name!r}")
    files = sorted(p for p in out_dir.iterdir() if p.is_file())
    for p in files:
        if not ASSET_NAME_RE.match(p.name):
            raise IntegrityError(f"refusing artifact name {p.name!r}")
        if not p.name.endswith(kinds):
            raise IntegrityError(f"{os_name} produced an unexpected file {p.name!r}")
    for kind in kinds:
        if not [p for p in files if p.name.endswith(kind)]:
            raise IntegrityError(f"{os_name} produced no {kind}")
    return files


def inspect(
    *, source: Path, out_dir: Path, os_name: str, tag: str, sha: str, expect_signed: bool, platform_check=None
) -> dict:
    parse_tag(tag)
    if not SHA_RE.match(sha):
        raise IntegrityError(f"{sha!r} is not a full commit SHA")
    head = _git(source, "rev-parse", "HEAD").stdout.strip()
    if head != sha:
        raise IntegrityError(f"built tree is {head}, but the release is for {sha}")

    check = platform_check or {
        "macOS": inspect_macos_dmg,
        "Windows": inspect_windows_msi,
        "Linux": lambda _p: (LINUX_SIGNING, {}),
    }[os_name]
    artifacts = []
    for path in collect_artifacts(out_dir, os_name):
        signing, facts = check(path)
        artifacts.append(Artifact(path.name, sha256_file(path), path.stat().st_size, signing, facts))

    if expect_signed and os_name != "Linux":
        unsigned = [a.file for a in artifacts if not a.signing.signed]
        if unsigned:
            raise IntegrityError(
                f"signing secrets are configured for {os_name}, but these artifacts carry no "
                f"verified signature: {unsigned}. Refusing to publish them"
            )
    record = {
        "schema": RECORD_SCHEMA,
        "os": os_name,
        "tag": tag,
        "commit": sha,
        "signing_expected": bool(expect_signed and os_name != "Linux"),
        "artifacts": [asdict(a) for a in artifacts],
    }
    (out_dir / f"BUILD-{os_name}.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return record


# ── assemble (release job) ────────────────────────────────────────────────


@dataclass
class Assembly:
    title: str
    notes: str
    assets: list[str]
    any_signed: bool
    all_signable_signed: bool


def assemble(*, dist: Path, tag: str, sha: str) -> Assembly:
    version = parse_tag(tag)
    if not SHA_RE.match(sha):
        raise IntegrityError(f"{sha!r} is not a full commit SHA")

    records: dict[str, dict] = {}
    for path in sorted(dist.glob("BUILD-*.json")):
        rec = json.loads(path.read_text())
        if rec.get("schema") != RECORD_SCHEMA:
            raise IntegrityError(f"{path.name}: unknown record schema {rec.get('schema')!r}")
        if path.name != f"BUILD-{rec.get('os')}.json" or rec["os"] in records:
            raise IntegrityError(f"{path.name}: record names OS {rec.get('os')!r}")
        if rec.get("tag") != tag:
            raise IntegrityError(f"{path.name}: built for tag {rec.get('tag')!r}, not {tag}")
        if rec.get("commit") != sha:
            raise IntegrityError(f"{path.name}: built from {rec.get('commit')}, not {sha}")
        records[rec["os"]] = rec
    if set(records) != set(EXPECTED_KINDS):
        raise IntegrityError(f"build records for {sorted(records)}; a release needs {sorted(EXPECTED_KINDS)}")

    listed: dict[str, tuple[str, dict]] = {}
    for os_name, rec in records.items():
        kinds = EXPECTED_KINDS[os_name]
        for a in rec["artifacts"]:
            name = a["file"]
            if not ASSET_NAME_RE.match(name) or not name.endswith(kinds):
                raise IntegrityError(f"BUILD-{os_name}.json lists an unexpected artifact {name!r}")
            if name in listed:
                raise IntegrityError(f"{name} is claimed by both {listed[name][0]} and {os_name}")
            if os_name == "Linux" and a["signing"]["signed"]:
                raise IntegrityError(f"{name}: Linux packages are never signed by this workflow")
            path = dist / name
            if not path.is_file():
                raise IntegrityError(f"{name} is recorded by {os_name} but was not downloaded")
            if sha256_file(path) != a["sha256"]:
                raise IntegrityError(f"{name}: SHA-256 differs from what the {os_name} build recorded")
            listed[name] = (os_name, a)

    on_disk = {p.name for p in dist.iterdir() if p.is_file() and not re.match(r"^BUILD-.*\.json$", p.name)}
    unlisted = sorted(on_disk - set(listed))
    if unlisted:
        raise IntegrityError(f"files no build recorded: {unlisted}")

    signable = [a for os_name, a in listed.values() if os_name != "Linux"]
    any_signed = any(a["signing"]["signed"] for a in signable)
    all_signed = all(a["signing"]["signed"] for a in signable)
    if all_signed:
        verdict = (
            "**Signing:** every macOS and Windows artifact carries a signature verified by CI (details below). "
            "Linux packages are unsigned; verify them against `SHA256SUMS.txt`."
        )
        suffix = ""
    elif any_signed:
        verdict = (
            "**Partially signed.** Only the artifacts marked `signed: yes` below carry a verified signature. "
            "Everything else is **unsigned**. Verify every download against `SHA256SUMS.txt`."
        )
        suffix = " (partially signed)"
    else:
        verdict = (
            "**Unsigned build.** None of these artifacts carries a verified code signature. "
            "macOS Gatekeeper and Windows SmartScreen will warn. Verify every download against `SHA256SUMS.txt`."
        )
        suffix = " (unsigned)"

    sums = "".join(f"{listed[n][1]['sha256']}  {n}\n" for n in sorted(listed))
    signing_lines = []
    for name in sorted(listed):
        os_name, a = listed[name]
        s = a["signing"]
        facts = "".join(f" {k}={v}" for k, v in sorted(a.get("facts", {}).items()))
        signing_lines.append(
            f"{name}: signed={'yes' if s['signed'] else 'no'} status={s['status']}{facts} — {s['detail']}"
        )
    signing_report = (
        f"Signing report for {tag} @ {sha}, generated by CI from the platform tools' output.\n"
        "Only a verified Developer ID (macOS) or a Valid Authenticode signature (Windows) is reported as signed.\n\n"
        + "\n".join(signing_lines)
        + "\n"
    )
    (dist / "SHA256SUMS.txt").write_text(sums)
    (dist / "SIGNING.txt").write_text(signing_report)

    notes = "\n".join(
        [
            "JARVIS desktop companion: **draft**. Review before publishing.",
            "",
            BOUNDARY_STATEMENT,
            GATE_STATEMENT,
            "",
            verdict,
            "",
            f"Built from `{sha}` (tag `{tag}`). Every artifact below was built from that commit; "
            "CI checked that the tag still pointed at it before and after this draft was created.",
            "",
            "## Signing (verified by CI)",
            "```",
            signing_report.rstrip("\n"),
            "```",
            "",
            "## SHA-256",
            "```",
            sums.rstrip("\n"),
            "```",
            "",
        ]
    )
    return Assembly(
        title=f"JARVIS Companion {tag[len(TAG_PREFIX):]}{suffix}",
        notes=notes,
        assets=sorted(listed) + ["SHA256SUMS.txt", "SIGNING.txt"],
        any_signed=any_signed,
        all_signable_signed=all_signed,
    )


# ── CLI ───────────────────────────────────────────────────────────────────


def _write_outputs(pairs: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in pairs.items():
                f.write(f"{k}={v}\n")


def _bool(text: str) -> bool:
    if text not in ("true", "false"):
        raise argparse.ArgumentTypeError("expected true or false")
    return text == "true"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("resolve")
    r.add_argument("--repo", type=Path, default=Path("."))
    r.add_argument("--event", required=True)
    r.add_argument("--ref", required=True)
    r.add_argument("--input-tag", default="")
    r.add_argument("--event-sha", required=True)
    r.add_argument("--remote", default="origin")
    r.add_argument("--main-branch", default="main")
    r.add_argument("--expect-sha")

    i = sub.add_parser("inspect")
    i.add_argument("--source", type=Path, required=True)
    i.add_argument("--out", type=Path, required=True)
    i.add_argument("--os", required=True, choices=sorted(EXPECTED_KINDS))
    i.add_argument("--tag", required=True)
    i.add_argument("--sha", required=True)
    i.add_argument("--expect-signed", type=_bool, required=True)

    a = sub.add_parser("assemble")
    a.add_argument("--dist", type=Path, required=True)
    a.add_argument("--tag", required=True)
    a.add_argument("--sha", required=True)
    a.add_argument("--meta-dir", type=Path, required=True, help="where NOTES.md, TITLE.txt and ASSETS.txt go")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "resolve":
            res = resolve(
                args.repo,
                event=args.event,
                ref=args.ref,
                input_tag=args.input_tag,
                event_sha=args.event_sha,
                remote=args.remote,
                main_branch=args.main_branch,
                expect_sha=args.expect_sha,
            )
            print(f"tag {res.tag} -> commit {res.sha} (version {res.version}, on {args.main_branch})")
            _write_outputs({"tag": res.tag, "sha": res.sha, "version": res.version})
        elif args.cmd == "inspect":
            rec = inspect(
                source=args.source,
                out_dir=args.out,
                os_name=args.os,
                tag=args.tag,
                sha=args.sha,
                expect_signed=args.expect_signed,
            )
            print(json.dumps(rec, indent=2, sort_keys=True))
        else:
            asm = assemble(dist=args.dist, tag=args.tag, sha=args.sha)
            args.meta_dir.mkdir(parents=True, exist_ok=True)
            (args.meta_dir / "NOTES.md").write_text(asm.notes)
            (args.meta_dir / "TITLE.txt").write_text(asm.title + "\n")
            (args.meta_dir / "ASSETS.txt").write_text("".join(f"{n}\n" for n in asm.assets))
            print(asm.title)
            print(asm.notes)
    except IntegrityError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
