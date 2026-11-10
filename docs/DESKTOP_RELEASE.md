# Desktop companion releases

How a `desktop-v*` release is cut, and what `.github/workflows/desktop-release.yml`
guarantees about it (OD-DESK-3, DESK-8). The checks live in
`desktop/scripts/release_integrity.py` and are tested by
`tests/foundation/test_desktop_release_integrity.py`.

Releases are **drafts** that the owner reviews and publishes by hand. The
workflow never publishes a release, never creates or moves a tag, and never
needs a secret to produce an unsigned build. **The Real-Data Gate is
unchanged:** every draft says that real user data is not cleared
(`docs/RELEASE_VALIDATION.md`).

## Cutting a release

1. Merge everything into `main`. Make sure `desktop/src-tauri/tauri.conf.json`,
   `desktop/package.json` and `desktop/src-tauri/Cargo.toml` all declare the
   version you are releasing (e.g. `0.1.0`).
2. Create an annotated tag on that `main` commit and push it:
   ```bash
   git fetch origin && git checkout --detach origin/main
   git tag -a desktop-v0.1.0 -m "JARVIS Companion 0.1.0"
   git push origin desktop-v0.1.0
   ```
   The push starts the workflow. To rebuild an existing tag later (for example
   after adding signing secrets), use **Actions → Desktop release → Run workflow**
   and give the tag. A manual run builds the **tag's** commit, whichever branch
   it is started from.
3. Review the draft: the title, the signing verdict at the top of the notes, the
   build commit, `SIGNING.txt` and `SHA256SUMS.txt`. Publish it only if all of
   them are what you expect.

## What the workflow checks

| Stage | Check | On failure |
|---|---|---|
| `resolve` | The tag name is `desktop-vMAJOR.MINOR.PATCH[-pre]`, with nothing a shell could interpret | stops, nothing built |
| | The tag **exists on the remote** (a tag only in the runner's clone does not count). It is peeled to a commit; a tag on a tree or blob is refused | stops |
| | On a tag push, the tag still points at the pushed commit | stops |
| | The commit is reachable from `main` | stops |
| | The three version files at that commit declare the tag's version | stops |
| `build` (×3) | The product checkout is exactly the resolved commit. No token is persisted while npm and cargo run | stops |
| | JS policy/boundary tests (Linux) and `cargo test` pass before packaging | stops |
| | Each artifact is hashed, and its signature is verified with the platform tool (see below). The result is recorded in `BUILD-<os>.json` with the tag and commit | stops |
| | A platform's signing secrets exist, but its artifact has no verified signature | **stops**. A broken secret never silently becomes an unsigned release |
| `draft-release` | The tag *still* points at the built commit | stops, no draft |
| | All three build records name the same tag and commit. Every file matches its recorded SHA-256, and no file is unlisted or missing | stops, no draft |
| | `gh release create --verify-tag`: aborts if the tag does not exist, so it can never create one | stops |
| | The tag is re-checked after the draft exists | **deletes that draft** (never the tag) and fails |

### What counts as signed

| Platform | Reported as signed only when | Never counted as signed |
|---|---|---|
| macOS | `codesign --verify --deep --strict` passes on the `.app` inside the DMG **and** `codesign -dvvv` shows a `Developer ID Application:` authority with a team identifier | ad-hoc and linker signatures. On Apple Silicon every binary carries one, and `codesign --verify` accepts it. Apple Development certificates also don't count |
| Windows | `Get-AuthenticodeSignature` reports `Valid` with a signer certificate | `NotSigned`, `UnknownError`, `NotTrusted`, `HashMismatch` |
| Linux | never: `.deb` and AppImage are distributed with SHA-256 checksums | — |

Gatekeeper acceptance and notarization stapling (app and DMG) are recorded as
facts next to the macOS result. When any macOS or Windows artifact is
unsigned, the release title ends in **(unsigned)** or **(partially signed)**
and the notes open with that verdict.

## Recommended repository settings (owner)

These are outside the workflow and are not applied by it:

* A **tag ruleset** for `desktop-v*` that blocks updates and deletions and
  restricts creation to the owner. The workflow detects a moved tag. The
  ruleset stops the tag from moving at all.
* An **environment** (e.g. `desktop-release`) holding the signing secrets, with
  the owner as required reviewer. Add `environment: desktop-release` to the
  `build` job when the secrets are added.
