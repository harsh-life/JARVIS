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
   after adding signing secrets), use **Actions → Desktop release → Run workflow**,
   choose **Branch: `main`**, and give the tag. A manual run is **refused from any
   other branch or tag**, and it builds the **tag's** commit, not main's tip.
3. If the `desktop-release` environment has required reviewers (see
   [Before the first release](#before-the-first-release-owner)), the build jobs
   wait for approval after `resolve` succeeds. Check the tag and commit in the
   `resolve` log, then approve.
4. Review the draft: the title, the signing verdict at the top of the notes, the
   build commit, `SIGNING.txt` and `SHA256SUMS.txt`. Publish it only if all of
   them are what you expect.

## What the workflow checks

| Stage | Check | On failure |
|---|---|---|
| `guard` | A manual run's ref is exactly `refs/heads/main`; a push run's ref is a `desktop-v*` tag. No checkout, nothing from the repository runs first. The run **fails** (it is never skipped, so never green) | stops, nothing else starts |
| `resolve` | A manual run is refused again unless started from `refs/heads/main`, and the commit it executes is on `main`. This also runs in the re-checks in `draft-release` | stops |
| | The tag name is `desktop-vMAJOR.MINOR.PATCH[-pre]`, with nothing a shell could interpret | stops, nothing built |
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

## Which code runs, and who can change that

`workflow_dispatch` cannot be limited to a branch in the workflow file, and
GitHub runs the copy of the workflow, and of `release_integrity.py`, that lives
on the ref the run was started from. Anyone with write access can start a run
from any branch. So:

| Layer | What it does | Can a branch edit it away? |
|---|---|---|
| `guard` job | Fails a manual run whose ref is not `refs/heads/main` | **Yes**: the branch's own copy of the workflow can drop it |
| `release_integrity.py resolve` | Refuses it again; requires the executed commit to be on `main` | **Yes**: same reason |
| Environment `desktop-release` | Deployment branches and tags limited to `main` and `desktop-v*`, plus required reviewers. Enforced by GitHub | **No** |

The first two stop mistakes (a run started from the wrong branch in the UI) and
make the intent testable. Only the environment stops a hostile or unreviewed
branch from reaching a signing secret, which is why the signing secrets must
live **only** in that environment, never as repository secrets. A repository
secret is readable by any branch's workflow.

Release tooling is always taken from `github.sha`: on a manual run that is the
tip of `main` (the guard), on a tag push the tagged commit (`resolve` requires it
to be on `main`). It is never taken from the `tag` input, a branch name or a PR
ref. `release_integrity.py` is only ever run from a `github.sha` checkout, never
from the product checkout at the tag's commit (a test pins both).

Residual risk: a tag push runs the tagged commit's own copy of the workflow.
That is why the tag ruleset below matters: only the owner should be able to
create `desktop-v*` tags, and only on merged `main` commits.

## Before the first release (owner)

The workflow is prepared for signing but contains **no secrets** and does not
create anything in GitHub. Do these in **Settings** before the first run or
before adding any signing secret:

1. **Create the environment `desktop-release` first.** Settings → Environments
   → New environment. The `build` jobs reference it by name, and GitHub
   **creates an unprotected environment** if a run references one that does not
   exist, which would run the builds with no approval. Set up the rules
   immediately, before any release run.
2. **Required reviewers:** add the owner (a second reviewer if there is one).
   Leave *Prevent self-review* off while the owner is the only reviewer,
   otherwise nothing can be approved.
3. **Deployment branches and tags:** *Selected branches and tags*, with a branch
   rule `main` and a tag rule `desktop-v*`. Without the tag rule a tag push
   cannot start its builds; without the branch rule a manual run from a feature
   branch would be accepted by this layer.
4. **Signing secrets, when you have them:** add them as **environment secrets**
   on `desktop-release`, not repository secrets:
   `APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`, `APPLE_SIGNING_IDENTITY`,
   `APPLE_ID`, `APPLE_PASSWORD`, `APPLE_TEAM_ID` (macOS) and
   `WINDOWS_CERTIFICATE`, `WINDOWS_CERTIFICATE_PASSWORD` (Windows).
   A test fails if any job other than `build` references a secret.
5. **A tag ruleset** for `desktop-v*` that blocks updates and deletions and
   restricts creation to the owner. The workflow detects a moved tag; the
   ruleset stops the tag from moving at all.

Check the result (read-only):

```bash
gh api repos/OWNER/REPO/environments/desktop-release \
  --jq '{rules: [.protection_rules[].type], branch_policy: .deployment_branch_policy}'
gh api repos/OWNER/REPO/environments/desktop-release/deployment-branch-policies \
  --jq '.branch_policies[] | {name, type}'
gh api repos/OWNER/REPO/environments/desktop-release/secrets --jq '.secrets[].name'
```

Expected: a `required_reviewers` rule, `custom_branch_policies: true`, the policies
`main` (branch) and `desktop-v*` (tag), and, only once you have added them, the
secret names listed above.

Every approval is a deliberate act: the Linux build waits for it too, so
nothing is built from the release path without the owner seeing the tag and
commit that `resolve` printed.
