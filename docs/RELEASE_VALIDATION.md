# Release validation — the Real-Data Gate

**Date:** 2026-09-28 · **Validated tree:** `main` @ `8b15e39` (Phase H + Stage 5: Judge J1–J4, operator console)
**Branch:** `claude/peaceful-fermi-rl8wa1` (evidence and measurements only; no feature)
**Authority:** `17_TEST_ACCEPTANCE_VALIDATION.md` §0/§4/§5 ("a requirement is met only when its test passes
with recorded evidence"), `14_SECURITY_BLAST_RADIUS.md` §4, PRD §38 (PILOT-004) and §39 (the 32 criteria),
`docs/DECISION_REGISTER.md`.

This document is the evidence record for the go/no-go on **real, non-disposable user data**. It replaces
the Phase H record (`git show 8b15e39:docs/RELEASE_VALIDATION.md`). Phase H evidence that still holds is
carried forward and re-measured where this run could. Anything not re-measured is marked.

> **Gate decision: CLOSED** (§P). No real, non-disposable user data until every blocker in §P is cleared.
> Disposable or test data only.

> **H-1 remediation (2026-09-28, same branch, §Q).** The owner decided H-1: the runtime store moves to
> PostgreSQL. It was implemented, and PRD #32 was re-measured: **MET on PostgreSQL** (§I). This clears
> blocker 2 of §P and nothing else. **The gate stays CLOSED.**

---

## A. Environment

| Item | Value |
|---|---|
| Host | Linux 6.18 (x86-64), 4 vCPU, 15 GiB RAM, cloud container. No USB bus, no `/dev/kvm` |
| Python | 3.11.15, fresh venv, `pip install -e ".[dev,memory]"` (mem0ai 2.2.1, chromadb 1.5.9, fastembed 0.8.1, dulwich 1.2.15, SQLAlchemy 2.1.1, FastAPI 0.141.1, pytest 9.1.1, import-linter 2.15) |
| Embedder | `python -m server.memory provision --config config.example.yaml`, downloaded fresh (the one sanctioned download) |
| Relational store | Gate run: SQLite via aiosqlite, file-backed per test (then the pilot store). **H-1 run (§Q):** PostgreSQL 16.13, local cluster, one fresh database per test (`HYPERMIND_TEST_DATABASE_URL`); asyncpg 0.31. Every suite also re-run on SQLite |
| Kernel confinement | Landlock available: BR-T2 rows 15–16 **measured**, not skipped |
| Java / Android | OpenJDK 21. Android SDK installed fresh for this run: platform 35, build-tools 35.0.0, platform-tools r37.0.1. Gradle 8.14.3 wrapper, cold cache |
| Android hardware | **None.** `adb devices -l` lists no device. No USB bus, no KVM, so no emulator either |
| Credentials | None of the owner's. Suites generate their own KEK and config (17 §6). No OIDC client, tunnel or FCM credential was available |

## B. Git SHA

- Validated tree: **`8b15e39`** (`Merge pull request #28`), clean working tree at the start.
- This run's commits sit on top of it and add tests and documents only (`git log 8b15e39..`). No production
  code changed.
- Stage 5 inventory against the tree: `server/evaluation/` (J1–J4) is wired in `server/composition/__init__.py`
  behind `evaluation.enabled` (default false). `server/dashboard/` and `GET /api/v1/admin/*` exist (read-only,
  superuser-only). `/api/v1/admin/control/*` holds the operator, break-glass and Judge controls. There is no browser
  UI for the console (OD-DASH-2). Migration head is `c9e5a1f3b7d4` (12 revisions).

## C. Test counts (this run)

| Suite | Result |
|---|---|
| Server (`pytest tests/ --ignore=tests/memory`, `HYPERMIND_REQUIRE_MEMORY_STACK=1`) | **1734 passed, 7 skipped, 0 failed** on `8b15e39` (434 s). The 7 skips are one parametrized conformance case: envelopes the server cannot mint, exercised by the reference guard and the Kotlin `DeviceGuard` |
| Memory release-blocking (`pytest tests/memory`, real Mem0 + Chroma) | **198 passed, 0 skipped**: Phase H's 196 plus this run's two PRD #32 measurements |
| BR-T2 (4 modules, `-s`) | **8 passed**; 41 rows, 14 REACHABLE, identical to Phase H |
| BR-T2 Judge / console (new, `-s`) | **1 passed**; 4 rows, 1 REACHABLE (row 38) |
| Import contracts (`lint-imports`) | **24 kept, 0 broken** |
| Migrations | `upgrade head → downgrade base → upgrade head` clean; `alembic check`: no drift |
| Guard mutations | **41/41 killed** (27 re-run + 14 new). The first run of the new mutants left 3 survivors, handled in §E |
| Android ktlint + detekt | pass (cold Gradle cache, fresh SDK) |
| Android `:contract` (JVM) / `:app` (Robolectric) | **54 / 206 tests**, 0 failures, 0 skipped |
| Android `assembleDebug` + `assembleDebugAndroidTest` + `lintDebug` | pass; Android lint **0 errors** (51 warnings, 2 informational) |
| APK secret scan (ANDC-T11) | `app-debug.apk` (58 MB, built this run): **no server secret found** |
| **Final tree (this branch's head)** | re-run after every change: contracts 24 kept; server **1768 passed, 7 skipped, 0 failed**. The +34 are 28 meta checks for the 14 new mutants, 4 grant-scope cases, 1 control-object test and 1 Judge BR-T2 module. Memory **198 passed**; BR-T2 **45 rows** (41 + 4) |
| Repository secret scan | tracked files: 0 findings (CI test). Git history: 1,178 blobs; 15 unmarked hits, all in superseded versions of test files, all synthetic (§J) |
| **H-1 run, PostgreSQL** (§Q) | server **1781 passed, 7 skipped, 0 failed**; memory **198 passed** (incl. PRD #32 S1/S2 and the pilot-concurrency suite); BR-T2 **45 rows, 15 REACHABLE**, row-for-row identical to SQLite; migrations round-trip clean, `alembic check` no drift |
| **H-1 run, SQLite** (§Q) | server **1787 passed, 7 skipped, 0 failed** (1786 + the probe fixed in `675daae`); memory **196 passed** (PRD #32 excluded: it refuses SQLite); migrations round-trip clean |
| **H-1 run, contracts / mutations** | `lint-imports` **24 kept, 0 broken**; guard mutations **44/44 killed** on PostgreSQL (41 + M42–M44, §Q.6) |
| **H-1 run, CI** | run 86 on `675daae`: `checks` (SQLite), `postgres` (reachability, migrations round-trip, server suite, memory suite, PRD #32 step) and `android` all **success** |

Test categories, kept separate (17 §1):

| Category | Evidence in this run |
|---|---|
| Functional | the server and memory suites |
| Release-blocking (17 §2) | §D matrix. Every RB id maps to a named test that passed, except TL-T6 (N/A: no MCP) |
| Adversarial (SEC-A..V, 14 §1) | §D.2 mapping. Every row has a behavioural test, except SEC-E (N/A) and SEC-H/I (governed by BR-T2) |
| Mutation | §E, 41 mutants |
| Real-store validation | MEM-T1 and MP-T1..T12 on real Mem0 + Chroma (§F) |
| Real-infrastructure validation | **partial.** A live server process with real Mem0, the console and a real superuser credential was exercised and audited (§J). The real authentication flow, the phone and push were **not** exercised: no OIDC client, stable hostname or FCM credential (§P) |
| Hardware validation | **not performed**: no Android device (§G) |

## D. Release-blocking tests (17 §2 / §5)

All of these ran green in this run's server or memory suite.

| Surface (17 §5) | RB tests | Where | Status |
|---|---|---|---|
| Identity / session (`03`) | AUTH-T1, T3, T4, T5, T7, T10 | `security_core/test_auth_identity_session.py`, `runtime/test_authorization_and_capabilities.py` | green; mutation M06/M07 (step-up), M16 (principal freshness) |
| **1. Cross-user isolation** | AZ-T1 (top), T2, T3, T5, T7, T9, T10; MEM-T1; FS-T5/T6 | `security_core/test_authorization.py`, `runtime/test_authorization_and_capabilities.py`, `memory/*`, `execution/test_fs_sandbox.py` | green; mutation M01–M03, M19, M20, M39–M41 |
| **2. Secret containment** | SS-T1, T2, T3, T4, T8; MP-T2; REPO-T1/T2 | `security_core/test_secretstore.py`, `runtime/test_secrets_and_providers.py`, `security_core/test_fail_closed_and_boundaries.py`, `execution/test_apk_secret_scanner.py`, `foundation/test_repo_secret_scan.py` | green; mutation M10, M11 |
| **3. Boundary non-bypass** | FS-T1/T2/T3/T9; NET-T2/T3/T8 | `execution/test_fs_sandbox.py`, `execution/test_net_egress.py` (behaviour-named: metadata, loopback, undeclared destination, zero egress), `execution/test_process_confinement.py` | green for the tools that exist. `system.restricted` children are kernel-confined. In-process adapters are `mediated` / `mediated_proxy` (BR-T2 rows 13–14, inside OD-A1 (a)). No third-party in-process tool code (no MCP). Mutation M12–M14, M27 |
| **4. Agent containment** | RT-T1, T2, T3, T4, T6; TL-T3, T5; AZ-T10 | `runtime/test_authorization_and_capabilities.py`, `runtime/test_resource_control.py`, `runtime/test_confirmation_boundary.py`, `security_core/test_confirmation.py`, `security_core/test_capabilities.py` | green; mutation M04, M05, M09, M18 |
| Device (`08`) | AND-T1, T4, T5 | `execution/test_android.py`, `execution/test_device_conformance.py`, `runtime/test_sensitive_app_gate.py` | green on the JVM with fakes and the reference guard. **On a device: not run** (§G) |
| Memory (`11`) | MEM-T1, T3, T6 | `memory/*` on the real store | green (§F) |
| Usage (`13`) | US-T1, US-T3 | metering assertions across `runtime/*`; `runtime/test_resource_control.py` | green |
| Config / repo (`15`/`16`) | CFG-T1, T3, T4, T5; REPO-T1, T2, T7 | `foundation/test_config.py`, `foundation/test_repo_secret_scan.py`, `foundation/test_module_boundaries.py`, `lint-imports` | green. CFG-T1 holds for the test and build path only (see PRD #28 below) |
| MCP (`07`) | TL-T6 | — | N/A: no MCP server exists (OD-TOOL-3 decided "none", 2026-10-02, register §2L); prohibition by absence |
| Blast radius (`14`) | BR-T2, BR-T4, INV-20 | §H | measured, with owner actions |

### D.1 PRD §39 RB criteria

| # | Criterion | Status |
|---|---|---|
| 2, 4 | credential once; revocation immediate | green (AUTH-T4, T5) |
| 6, 8 | private stays private; server-side membership only | green (AZ-T1, AZ-T3, AUTH-T7) |
| 7 | explicit, audited sharing | green |
| 10, 11 | engine authorizes every action; bounds stop explicitly | green (RT-T1, RT-T2) |
| 14 | device capability = enumerated ops only | green (AND-T1, JVM). Not on a device |
| 16 | confirmation, no timeout approval; floor never confirmable | green (RT-T3, RT-T4, TL-T5) |
| 17, 18 | tool cannot escape fs sandbox / declared egress | green for `system.restricted` (kernel); in-process = BR-T2 rows 13–14 (OD-A1 (a)) |
| 19, 20 | no secret leak; no superuser path for users, agent or tools | green (SS-T1, SS-T4, M10, M17, M36, §J) |
| 22 | Mem0 isolation under concurrency | green (§F, `test_pilot_concurrency.py`) |
| 23 | vault distinct from Mem0 | green (MEM-T6, VAULT-003 contract) |
| 24 | works with intelligence disabled | green (CFG-T5) |
| 26 | speaker identity never authorizes | green (M22) |
| 28 | fresh clone on the cloner's own credentials | green for **test and build**. **End-to-end self-host not exercised** (real OIDC client, tunnel, phone pairing: §P) |
| **29** | dashboard shows usage/health/audit, no secret or unredacted PII by default | **green: new evidence.** DSH-T1..T5 (`tests/dashboard/`). Mutations M28–M31 killed. JSON API only; no browser UI (OD-DASH-2) |
| 30 | reminder needs a user-given reason | green (`tests/scheduler/`) |
| **31** | SEC-A..V pass; BR-T2 run and reviewed | behavioural suite green (§D.2). BR-T2 re-run (§H). Its reachable rows outside OD-A1 wait on the owner |
| **32** | ~10-device load with per-principal fairness | Gate run: **NOT MET** on SQLite. **H-1 run: MET on PostgreSQL** (§I, §Q) |

### D.2 Adversarial suite (SEC-A..V → tests)

Only five SEC rows carry their label in a test. Each row was mapped to the tests that exercise its expected block:

| Row | Expected block | Test |
|---|---|---|
| A probe others' resources | 404 anti-enum | AZ-T3 (`test_authorization.py`); M03 |
| B forged `user_id`/`graph_id` | identity re-derived | AUTH-T7; labelled SEC-B |
| C prompt injection | action denied unless authorized | RT-T1; labelled SEC-C |
| D malicious tool output | no action from embedded text | labelled SEC-D (`runtime/test_secrets_and_providers.py`), device-observation quoting (`execution/test_device_observations.py`) |
| E rogue MCP | — | **N/A**: no MCP (OD-TOOL-3) |
| F compromised tool process | no fs or egress escape | `test_process_confinement.py` (Landlock + seccomp), FS-T9; in-process = BR-T2 13–14 |
| G agent self-escalation | floor by absence | TL-T5, RT-T4; M09 |
| H, I app / server RCE | — | governed by BR-T2 and OD-A1 (§H), not pass/fail |
| J stolen device credential | revocation | AUTH-T5; M16 |
| K stolen access token | short TTL, immediate revocation | AUTH-T8 |
| L stolen API key | handle-only, no exfil egress | labelled SEC-L; SS-T2, MP-T2 |
| M malicious graph member | D4 / MEM-T1 | AZ-T1, MEM-T1; M01, M19 |
| N malicious shared content | content is data | injection fixtures in `memory/test_memory_config_and_boundaries.py`, `memory/test_vault.py`, `execution/test_device_observations.py`, `scheduler/test_agent_path.py` |
| O SSRF | metadata, loopback, private blocked | `execution/test_net_egress.py`; M12, M13 |
| P traversal / symlink / zip-slip | rejected | FS-T1/T2/T3; M14, M27 |
| Q cross-user graph | D1 404 | AZ-T3; M03 |
| R cross-user memory | filtered | MEM-T1; M19 |
| S secret in logs | never logged | `caplog` assertions in `runtime/test_secrets_and_providers.py`, `voice/*`, `security_core/test_break_glass_registry.py`, `memory/test_mem0_provider.py`; M11 |
| T scheduler abuse | quota-capped | SCH-T5 (`scheduler/test_jobs_api.py`) |
| U model-call abuse | per-request caps | `runtime/test_resource_control.py::test_max_model_calls_is_enforced` |
| V budget exhaustion | refused at budget | labelled SEC-V |

## E. Mutation tests

`tests/tools/guard_mutations.py` disables one deterministic guard at a time with one exact source edit,
runs that guard's defending tests, and requires them to **fail**. A compile or collection error never counts
as a kill. `tests/foundation/test_guard_mutations_meta.py` keeps every anchor present in CI (83 checks).

This run re-ran Phase H's 27 mutants on the Stage 5 tree and added **14** (M28–M41). Before this run, no
Stage 5 guard and no grant-scope guard was mutation-tested.

**Result on this run: 41 / 41 killed.**

| Mutant | Guard disabled | This run |
|---|---|---|
| M01, M02, M03 | AuthZ D4 visibility · D4 graph scope · D1 membership | killed |
| M04, M05 | Confirmation token binding: arguments · session | killed |
| M06, M07 | Step-up: signature · challenge expiry | killed |
| M08 | Exact-device routing | killed |
| M09 | Absolute floor | killed |
| M10, M11 | SecretStore agent denial · secret filter | killed |
| M12, M13 | Egress: metadata · loopback | killed |
| M14, M27 | Sandbox leaf: read/listing TOCTOU · write | killed |
| M15 | Sensitive-app gate | killed |
| M16 | Principal freshness (revoked device) | killed |
| M17 | Break-glass superuser | killed |
| M18 | Task-mode ceiling | killed |
| M19 | Hydration `readable()` re-check | killed |
| M20 | Per-user idempotency namespace | killed |
| M21 | Push payload content-free | killed |
| M22 | Voice never an authorization signal | killed |
| M23 | Scheduler cannot reach the runtime | killed |
| M24, M25, M26 | Kotlin `DeviceGuard`: grid toggle · expiry · wrong device | killed (Gradle, this run's SDK) |
| **M28** | Console: user content shown instead of redacted (DSH-T4) | killed |
| **M29** | Console: a credential in a setting no longer masked (DSH-T3) | killed |
| **M30** | Console: unredacted view not audited before it reads (DSH-T4) | killed |
| **M31** | Console: a view no longer requires the superuser principal (DSH-T2) | killed |
| **M32** | Judge: a post-hoc evaluation may stop a task (JDG-B2) | killed |
| **M33** | Judge: a stop honoured while the stop switch is off (JDG-B2) | killed |
| **M34** | Judge: a secret in the trace reaches the Judge (JDG-T6) | killed |
| **M35** | Judge: a candidate may cite another task as evidence (JDG-T7) | killed |
| **M36** | Judge: an improvement decision without a verified superuser (JDG-T9) | **survived first; killed after a new test** |
| **M37** | Judge: its own budget no longer refuses a paid call (JDG-T8) | killed |
| **M38** | Judge: the operator can switch on stop requests the config withholds (JDG-B4) | killed |
| **M39** | AuthZ D5: a request without the required grant passes | **survived first; killed with the correct defender** |
| **M40** | Task-scoped grant usable by a principal other than the consenting user | killed |
| **M41** | Device-scoped grant matches any device | **survived first; killed after a new test** |

**What the first run found.** Three of the new mutants survived:

- **M36:** `EvaluationControl` checks for a verified superuser itself, beneath the HTTP gate. Every test went
  through HTTP, where `get_superuser` refuses first, so the inner check was never pinned.
  `test_the_control_object_itself_refuses_without_a_verified_superuser` now calls the control directly with no
  principal and with an unverified grant. It fails without the check.
- **M39:** D5 *is* defended. Removing it fails four runtime tests (e.g.
  `test_revoking_a_grant_stops_the_next_operation_even_if_approved`). The engine-level files first named as its
  defenders do not pin it, so the mutant now names the runtime file.
- **M41:** the candidate-id SQL already narrows grants to the caller's own ids, so the scope check never saw a
  mismatched row. `test_a_grant_keyed_on_the_wrong_kind_of_id_never_matches` plants rows keyed on the caller's own
  ids of the wrong kind (e.g. a DEVICE grant keyed on the session id) and requires a denial. It fails without the
  check.

No production code changed. Each guard held. Two had no test that would notice their removal, and now each does.

## F. MEM-T1 (real Mem0 store)

- **Path validated:** request → gateway authentication → engine authorization → `MemoryProvider` query with
  visibility pushed into the store filter → the engine's own `readable()` re-check with live membership →
  bounded hydration → model context. This runs through the production composition root on the **real**
  Mem0 2.2.1 + Chroma 1.5.9 store. No store was faked.
- **Users:** 2-user and 3-user fixtures in every MEM/MP suite. `test_pilot_concurrency.py` runs **ten users on
  ten devices in one shared graph, concurrently**: writes, shares, listings, targeted searches aimed at the
  next user's private facts, and agent hydration whose prompt names every user's words.
- **Result:** 198 passed, 0 skipped, with `HYPERMIND_REQUIRE_MEMORY_STACK=1`, so a missing store is a failure,
  not a skip. No user's private fact reached another user through the API, search or hydration. M19
  (hydration re-check removed) is killed.
- **Recorded separately:**

| Question | Finding | BR-T2 row | Inside OD-A1 (a)? |
|---|---|---|---|
| Application-level containment | contained: API, search, hydration, correction, share, delete, vault, after graph-leave | 18–22 | n/a (holds) |
| Direct-store reach by in-process code | **REACHABLE** | 23 | yes |
| At-rest exposure (live facts) | **REACHABLE**: plaintext, 0700 directories | 27 | **no**: OD-MB-4 |
| Deletion: text | contained: removed from every store file | 26 | n/a |
| Deletion: index | **REACHABLE**: embedding vectors kept in HNSW until a rebuild | 28 | **no**: OD-MB-4 |
| Secrets in the store | contained: write gate refuses secret-shaped text | 25 | n/a |

MEM-T1 passes. Memory **at rest** stays **BLOCKED** on OD-MB-4 for any deployment with `memory.enabled: true`.

## G. Android physical-device validation

**Not performed. BLOCKED on hardware.**

- Evidence of absence: `adb devices -l` (platform-tools r37.0.1) lists no device. The host has no `/dev/bus/usb`
  and no `/dev/kvm`, so neither a phone nor an accelerated emulator can attach.
- docs/23 §9 `[LOCKED]` requires AND-T1..T8 on a real device and instrumentation tests on a budget-class phone.
  **That requirement is unmet.**
- What did run on this tree: `:contract` 54 tests and `:app` 206 Robolectric tests, 0 failures. Also ktlint, detekt, Android lint, the APK build and the APK
  secret scan (§C). These cover the Kotlin guard, the wire contract, perception validation, the presentation
  boundary, and push/voice/overlay authority checks against fakes and Robolectric.
- None of the gate's device checklist ran on hardware. That covers login and session handoff, device identity,
  WebSocket reconnect, push wake and token binding, task waiting, Shizuku unavailable and returning, fresh
  reauthorization, expiry, confirmation, biometric step-up, per-app grants, sensitive-app restrictions,
  Accessibility perception, app metadata, OCR fallback, screenshot fallback, `isPassword` redaction,
  FLAG_SECURE, screenshot refusal, typed Shizuku force-stop, absence of ADB/shell authority, no queued
  operation offline, revocation, wrong user or wrong device, and push, voice or overlay authorizing. Each item
  has JVM or Robolectric evidence listed in the Phase H record (`8b15e39`, §8–§9). Hardware evidence is **none**.
- BR-T2 §3d rows 30–32 against the Kotlin guard on a phone: **not measured**.
- Also needed first: a stable HTTPS hostname for the Google redirect URI and the App Link (NEXT_BUILD_INDEX §6
  item 3), a real OIDC client, and, for push, an FCM credential.

## H. BR-T2 final results

Re-run in this gate run on `8b15e39`, plus the new Judge rows. Attacker models: **app-RCE** (code in the server
process, OD-A1's class), **authorized** (ordinary users and the operator using paths the deterministic layer
allows), **at-rest** (store files without the process). The per-row "why" is in `docs/OD_A1_BR_T2.md` §3–§3e.

| Rows | Surface | Reachable | Inside OD-A1 (a) | Blocks real data | Owner action |
|---|---|---|---|---|---|
| 1–11 | relational store, secrets, superuser | 2, 3, 6, 11 (app-RCE) | yes | no (accepted) | none further |
| 12–17e | files, egress, process, break-glass | 13, 14 (app-RCE); 17c, 17d (authorized, *during* an active break-glass record) | 13, 14 yes; 17c/d deliberate, audited (OD-EXEC-2 ratified form) | no. Each break-glass activation with real data is a cross-user exposure event | transcribe OD-EXEC-2 (OD-BG-1) |
| 18–28 | memory | 23 (app-RCE); 27, 28 (at-rest) | 23 yes; **27, 28 no** | **yes**, when memory is enabled | **OD-MB-4** |
| 29–37 | Android device | 30, 34 (app-RCE); 36 (at-rest) | 30, 34 yes; **36 no** | **yes**, when push is enabled | **H-2** |
| **38–41** | **Judge / console (new)** | **38 (authorized, operator-approved)** | **no** | **yes**, when the Judge is enabled | **OD-JDG-5** |
| 30–32 on hardware | Kotlin guard on a phone | not measured | — | yes (§G) | hardware |

**No ordinary-user authorized path reaches another user's data.** Row 38 is reachable only through an operator
approval. The distinction that must be kept: the ordinary authorization suite proves **logical** isolation for
every caller that goes through the engine. BR-T2 proves that isolation does **not** hold against code running
inside the server process (rows 2, 3, 6, 11, 13, 14, 23, 30, 34). Nothing in this document claims cross-user
isolation under application RCE (INV-20).

## I. PRD #32 service / load result

Measured by `tests/memory/test_prd32_service_measurement.py`: the production composition root, the real Mem0
store, ten users in one shared graph, model latency a sleep. Since the H-1 run it is an **acceptance test**:
it asserts the criterion and refuses to run on any store but PostgreSQL (`HYPERMIND_PRD32_BASELINE=sqlite`
reproduces the baseline). Pass conditions, fixed before the fix was written (Slice A, `3dd0ab6`): S1 all 20
requests served, no cross-user leak, **zero** `503 storage`, every user's worst wait ≤ 3.0 s (6 × the model
call); S2 all reads served, **every** write served (`201`) while the long task is still running, zero `503`,
median write latency ≤ 1.0 s. Retryable `429`s from the configured concurrency caps are retried, as a client
does.

**Before — gate run, SQLite, one transaction per request (`8b15e39`):**

| Scenario | Result |
|---|---|
| S1: 10 users at once, one task (0.5 s model call) + one memory write each | all 20 served, 0 lost, 0 leaked. Wall clock **11.6 s** (one task alone: 0.5 s). Task time-to-served p50 **8.7 s**, max 11.6 s. Memory write p50 7.3 s. **15 retryable `503 storage`** across **9/10 users**; 6 `429` from the concurrency caps |
| S2: one user's task makes a 6 s model call; 9 other users each try once | memory reads 0/9 refused. Memory writes **9/9 refused `503 storage`** after a median 5.2 s wait. After the task, 9/9 served |

Cause: one user's task held SQLite's single write lock for its whole request, so every other user's write
waited the 5 s busy timeout and was refused while it ran.

**After — H-1 run, PostgreSQL 16, short task transactions (`675daae`), three consecutive runs:**

| Scenario | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| S1 wall clock (one task alone: 0.5 s) | **1.80 s** | 1.75 s | 1.80 s |
| S1 task time-to-served p50 / max | **1.16 s / 1.80 s** | 1.18 / 1.75 s | 1.14 / 1.80 s |
| S1 memory write time-to-served p50 / max | **0.75 s / 0.83 s** | 0.80 / 0.88 s | 0.72 / 0.82 s |
| S1 worst per-user wait (limit 3.0 s) | **1.80 s** | 1.75 s | 1.80 s |
| S1 `503 storage` | **0** (0/10 users) | 0 | 0 |
| S1 `429` (all `global_concurrency`: 10 users, 8 configured slots) | 10 | 10 | 9 |
| S1 served | **20/20** | 20/20 | 20/20 |
| S2 long task end to end (6.0 s model call, never cut short) | 6.13 s | 6.13 s | 6.13 s |
| S2 other users' reads served | **9/9** (median 0.21 s) | 9/9 (0.18 s) | 9/9 (0.19 s) |
| S2 other users' writes served, while the long task ran | **9/9, 0 × `503`** | 9/9, 0 | 9/9, 0 |
| S2 write latency p50 / max | **0.51 s / 0.51 s** | 0.51 / 0.51 s | 0.56 / 0.57 s |

Isolation under the same load (`test_pilot_concurrency.py`) holds on PostgreSQL. Its flood test now also
asserts the half that used to fail: while a flooder's admitted tasks are running, another user's task is served
at once.

**Status: PRD #32 MET on PostgreSQL.** Every S1/S2 condition holds, in every run, with no weakening of the
scenario. Ten users, the 0.5 s and 6 s model calls and the memory writes are unchanged. A single `503` fails
either scenario. The S2 long call is now *longer* than in the baseline test: it always runs its full 6 s instead of
being released once the others were served. The `429`s are the configured global cap of 8 concurrent tasks,
applied equally to every principal, and are explicit and retryable.

**What fixed it, and a nuance to keep.** The engine alone was not the fix. A PostgreSQL store still held one
transaction, and one pooled connection, per task across every model call. The change that removes the
starvation is the transaction scope (§Q.3). With it in place, the same synthetic acceptance also passes on SQLite
(informational run: S1 1.96 s wall clock, 0 × `503`; S2 9/9 writes served during the task). SQLite is still one
writer at a time: every commit serializes. PostgreSQL is the store the owner chose, and the one validated in CI.
It is the supported pilot store. SQLite stays for development.

## J. Secret audit

| Place | Method | Result |
|---|---|---|
| Tracked files (all) | `foundation/test_repo_secret_scan.py`: server patterns + Groq/FCM formats, TEST-ONLY marker required | 0 findings |
| Git history (all refs, every blob) | the same patterns plus service-account JSON, HF/Anthropic tokens, `HYPERMIND_KEK=` / `HYPERMIND_SUPERUSER_TOKEN=` assignments | 1,178 blobs. 15 unmarked hits, all in **superseded versions of test files** from before `3538f44` / `31a0901` added the markers. All synthetic and self-describing: a key literally named "planted secret value", AWS's documented example access-key id, a GitHub-token shape built from a repeated three-character string, a "hunter2" database password, a key literally named "literal-looking API key", and a truncated PEM header. **No real credential.** The current versions are marked TEST-ONLY |
| APK | `tests/tools/check_apk_secrets.py` on this run's `app-debug.apk` | no server secret found |
| Logs / traces | SS-T1, MP-T2 and `caplog` assertions; Judge trace redaction (JDG-T6, M34) | green |
| Dashboard | DSH-T3 (no secret value; handles and resolvability only), DSH-T4 (content redacted) | green; M28–M31 |
| Usage records / audit | SS-T1, `test_the_token_is_never_written_to_the_audit_trail`, idempotency copy nulls the confirmation token (OD-IDEM-2) | green |
| Memory store / vault | write gate refuses secret-shaped text (MP-T5, BR-T2 row 25) | green |
| Database | SecretStore ciphertext only; KEK external (BR-T2 rows 7–8). Plaintext at rest: memory facts (27), push token (36), task responses and Judge notes (row 2 class) | as recorded in §H |
| Screenshots / OCR / audio | never persisted (BR-T2 row 37; VOI-B7, `audio_retained` always false) | green (JVM). Device-side not validated (§G) |
| Live server run (partial real-integration smoke) | a real `uvicorn` process with memory on (real Mem0), a freshly generated KEK and a TEST-ONLY superuser credential. All 11 console views were called as superuser; unauthenticated, Bearer-scheme and wrong-credential calls were refused `401`. Then the server log, `hypermind.db`, the Mem0 store and every console response were scanned for both values, with a planted-copy self-check | **neither value found anywhere**; audit rows carry only the credential's fingerprint; the configuration view shows `{"handle": "env:HYPERMIND_KEK", "resolves": true}` |
| Temporary / runtime files | this run's scratch DBs, logs and Mem0 directories stayed in the session scratchpad and per-test tmp dirs. The tree's `data/` holds only the provisioned public embedding model (git-ignored) | nothing committed |
| Fixtures | 17 §6: test secrets clearly marked | TEST-ONLY markers enforced by the tracked-file scan |

## K. Judge validation

- JDG-T1..T9 present and green (`tests/evaluation/`, 9 IDs, 23 labelled assertions). The Judge is **off** by
  default (`evaluation.enabled: false`, `provider: null`, `may_request_stop: false`, `budget: 0.0`).
- Authority: the import contracts "The Judge is never an authority", "The runtime never depends on the Judge"
  and "The Judge opens no process or socket" all hold. Stop gating, trace redaction, evidence scoping,
  superuser-only approval, own budget and config caps were each mutation-tested (M32–M38).
- **Finding:** BR-T2 row 38 (§H). Approved guidance is global and screens only for secret shapes, so one user's
  content can reach every user through an operator approval made from the redacted console. **OD-JDG-5, owner.**
  It is mandatory before the Judge runs on real data.
- Live monitoring was not exercised against a real provider. No Judge provider is chosen (OD-JDG-3).

## L. Dashboard validation

- DSH-T1..T5 present and green (`tests/dashboard/`): every route is GET and superuser-only (asserted at import
  and by an import contract). No ordinary or forged credential reaches `/admin/*` or `/admin/control/*`.
  Configuration shows handles and resolvability. User content is redacted, and the unredacted view is audited
  before it reads. Banners persist.
- Mutation-tested: M28 (redaction), M29 (secret scrub), M30 (audit before read), M31 (superuser dependency).
- PRD #29 therefore has evidence (it had none at Phase H). OD-DASH-1 (ratify the split) stays open. The code
  conforms to locked DASH-002 as written, so #29 does not depend on it.
- Exercised on a live server process (no user data): all 11 views as superuser, the `401` refusals, and the
  configuration view's handle-only rendering (§J). Not validated: a browser console (none exists, OD-DASH-2).
- Pre-existing residual: a routing `405` is reported as `500 internal_error` (§2G). Not fixed. The locked error
  registry has no code for it, so a fix is a registry decision, not a defect fix. No handler is reached.

## M. Remaining owner decisions

Classification: **A** already decided · **B** resolvable from locked text · **C** owner must decide ·
**D** no longer relevant. "Mandatory" means the gate cannot open for a deployment that uses the feature until it
is decided.

| ID | Class | Mandatory for real data? | What the owner must choose |
|---|---|---|---|
| OD-A1 | A | decided: (a) for the in-process class | nothing. Rows outside the class are listed below |
| **OD-MB-4** | C | **yes**, if `memory.enabled: true` | accept plaintext live facts and retained deleted vectors for the pilot, or require encryption and/or an index rebuild first |
| **H-2** | C | **yes**, if `android.push.provider: fcm` | accept the plaintext push token, or encrypt the column. With `none` the server stores no token |
| H-1 | **A — decided (H-1 run)**: OWNER DECISION PostgreSQL, `DECISION_REGISTER.md` §2I | no longer: #32 is met on PostgreSQL (§I) | nothing. Deploy on PostgreSQL (§Q) |
| **OD-JDG-5** (new) | C | **yes**, if `evaluation.enabled: true` | how approved guidance may carry user content (§K) |
| matrix §5.1 sensitive-app lists | C | not for safety: the lists ship empty, so no app is UI-controllable. Yes for Android UI control | which apps are sensitive, and their tiers |
| OD-JDG-3 | C | yes, if the Judge is enabled with a cloud provider (traces leave the host) | pilot Judge provider |
| OD-TOOL-1 tier table | C | no: RB #16 holds on the proposed table. The owner signs it | per-operation tiers |
| OD-MT-2 / C1 | C (conflicts with LOCKED PRD §19) | no: budgets 0.0 fail closed. A remote default sends user data to a provider | remote vs local default; amend PRD §19 |
| OD-EXEC-2 | A (docs/20 ratified form) | — | — |
| OD-BG-1 | C (transcription) | no | authorize the register edit |
| OD-EXEC-2 global `unconfined` text in §2B | D | — | the switch no longer exists (BG-T2) |
| OD-AND-2/3/4/5, OD-VOI-1, VOI-B3, OD-SCH-1/2, OD-SUP-1/3, OD-BG-2/3, OD-MEM-A/B, OD-VLT-1, OD-AUTHZ-1, OD-JDG-1/2/4, OD-DASH-1/2, OD-02, OD-USE-1..3, OD-RT-1..3 | C (implemented recommendation or PROPOSED) | no: each is off, empty, or in its more restrictive reading by default | ratify or change. OD-AUTHZ-1 (shared facts stay on leave) and OD-DASH-1 are the ones that touch real-data semantics |
| OD-DP-9 | C | no: nothing depends on it | ratify DecisionProvider at all |
| Stable HTTPS hostname (NEXT_BUILD_INDEX §6.3) | infrastructure | yes, for Android login and the smoke test | provide one |

No decision was closed in this run. Implemented recommendations are not ratifications.

## N. Remaining known limitations

- One server process, one uvicorn worker. The task registry, breaker and break-glass records are in-process
  (RUNNING_RUNTIME §4a).
- SQLite single writer (§I): SQLite is for development. The pilot store is PostgreSQL (H-1, §Q).
- H-1 run: the idempotency in-flight guard and the usage-admission reservations are **in-process**. This is
  correct only with one server process, which is already required (above). With several processes, a same-key
  retry that reaches a *different* process while the original runs would run the task a second time. The table's
  primary key then refuses its stored result, and it answers `500`. Admissions would not see other processes'
  calls in flight either. **Run one process.**
- H-1 run: a PostgreSQL server that goes away mid-run surfaces as `500 internal_error`: a refused connection
  is not a DBAPI error and is not mapped. Transient conflicts the server *reports* are mapped to a retryable
  `503`. At startup an unreachable or misconfigured database stops the server (exit 3), which fails closed.
- H-1 run: a task's committed prefix (its row, authorization decisions, audit and metered spend) survives an
  unexpected mid-task failure instead of being rolled back. The restart reconciliation closes a row left
  `running`. Within the same process, such a row stays `running` until the next restart.
- H-1 run: the memory API's read paths (list, search, recall) and voice's server-side STT/TTS (off by default,
  OD-VOI-1) still hold their request's transaction across the provider call. It is read-only apart from the
  caller's own session row. On PostgreSQL that takes one pooled connection for the call and no lock another user
  needs. The task path, the memory write path and the Judge hold none.
- H-1 run: PostgreSQL enforces foreign keys, except on `audit_events`' id columns, which are plain identifiers
  by design (migration `a2d6e8f4c0b9`). SQLite foreign keys stay off.
- `mediated` fs and `mediated_proxy` egress bind this codebase's adapters, not the process (BR-T2 13–14).
- A restart fails interrupted tasks closed. Paused actions and transcripts are volatile.
- FilesystemSandbox does synchronous I/O on the event loop, bounded by quotas.
- Evaluations and candidates store model-written notes about a user's task (owner-private; row 2 class).
- Routing `405` is reported as `500` (§L).
- Android: SDK network behaviour (ML Kit / Firebase data transport) not captured on a device.

## O. OD-A1, explicitly

OD-A1 is **decided: option (a), accepted for the pilot**, owner decision of 2026-09-22. It is a **measured
blast-radius acceptance, not an isolation claim**. It accepts exactly one class of residual: *a compromised live
server process reaches what that process can already reach* (BR-T2 rows 2, 3, 6, 11, 13, 14, 23, 30, 34). It
does **not** cover:

- at-rest exposure (rows 27, 28, 36): no process is needed;
- authorized paths (row 38): no compromise is needed;
- unmeasured surfaces (rows 30–32 on hardware).

Those go to the owner (§M). OD-A1 being decided does **not** open the gate. 17 §5 also requires every RB test
green. #32 is now met on PostgreSQL (H-1, §I), but the other blockers in §P stand.

## P. Gate decision

**REAL-DATA GATE: CLOSED.**

| # | Blocker | Evidence | Impact | Required action | Type | Prevents real data |
|---|---|---|---|---|---|---|
| 1 | Android release-blocking set not run on a physical device (docs/23 §9 `[LOCKED]`); BR-T2 30–32 not on hardware | §G: `adb devices` empty, no USB or KVM | device control, perception and step-up unvalidated on real hardware | run AND-T1..T8 plus instrumentation on a budget-class phone; re-run BR-T2 30–32 | **hardware** | **yes** |
| ~~2~~ | ~~PRD #32 per-principal fairness not met~~ **Cleared by the H-1 run**: owner decision PostgreSQL, implemented, #32 **MET** on PostgreSQL | §I (after), §Q | — | deploy on PostgreSQL, one server process | — | **no longer** (for a PostgreSQL deployment) |
| 3 | Memory at rest: plaintext facts, retained deleted vectors | BR-T2 27–28 | a stolen disk or backup reveals memory | OD-MB-4 | **owner decision** | **yes**, if memory is enabled |
| 4 | Push token at rest | BR-T2 36 | token readable from a stolen DB (usable only with the FCM credential, for a content-free wake) | H-2 | **owner decision** | **yes**, if push is enabled |
| 5 | Judge guidance carries one user's content to all users | BR-T2 38 | cross-user disclosure through an operator approval | OD-JDG-5 | **owner decision** | **yes**, if the Judge is enabled |
| 6 | Real-integration smoke not performed end to end. Only a live server, Mem0 and console run was done (§J) | no OIDC client, stable hostname, FCM credential or phone | the integrated path under real auth has never run end to end | provide the infrastructure; run the smoke with disposable pilot accounts | **infrastructure / hardware** | **yes** |
| 7 | PRD #28 end-to-end self-host not exercised | test/build path green only | first real self-host is unproven | same as 6 | **infrastructure** | **yes** |

Items 3–5 are conditional on a feature that is off by default. Items 1, 6 and 7 are not (item 2 is cleared by
the H-1 run). The gate therefore stays closed even for a deployment with memory, push and the Judge all turned
off.

**REAL-DATA GATE: CLOSED. Disposable or test data only.**

---

## Q. H-1 remediation — the runtime store moves to PostgreSQL (2026-09-28)

### Q.1 Owner decision

*"Move the runtime relational store to PostgreSQL to remove the SQLite single-writer bottleneck, then re-run the
PRD #32 ~10-device fairness acceptance test."* It is recorded as **OWNER DECISION — PostgreSQL** in
`DECISION_REGISTER.md` §2I, with rationale, consequences, validation method and status. No other decision was
closed.

### Q.2 Implementation (existing SQLAlchemy/Alembic path; no second schema)

| Change | Where | Commit |
|---|---|---|
| PRD #32 as an acceptance test, failing on SQLite for the intended reason | `tests/memory/test_prd32_service_measurement.py` | `3dd0ab6` |
| `database_url`: `postgresql+asyncpg` or `sqlite+aiosqlite` only; a URL with a password is refused (SECRET-004: `PGPASSWORD` / `~/.pgpass`); the refusal names the field and rule, never the value, and is not chained | `server/config/schema.py`, `server/config/loader.py` | `cec7218` |
| A boolean check constraint PostgreSQL can evaluate (`NOT is_authorization_signal`) | model + initial migration | `cec7218` |
| Explicit `flush()` where a child row's foreign key needs its parent first (PostgreSQL enforces FKs) | `server/secrets/store.py`, `server/auth/sessions.py` | `cec7218` |
| Migration `a2d6e8f4c0b9`: `audit_events` user/device/session/graph become plain identifiers (audit rows deliberately name ids that do not, or no longer, exist) | `server/storage/migrations/versions/` | `cec7218` |
| Transient store conflicts → retryable `503 dependency_unavailable` (`storage`) | `server/storage/errors.py`, `server/gateway/errors.py` | `cec7218` |
| Short task transactions + concurrency guards (§Q.3) | runtime, composition, usage, idempotency | `eef7a85` |
| Migration-time `HYPERMIND_DATABASE_URL` gets the same driver / no-password check | `server/storage/migrations/env.py` | `eef7a85` |
| CI `postgres` job | `.github/workflows/ci.yml` | `69d0761` |

The connection string was not simply swapped: on PostgreSQL alone, before §Q.3, the S2 condition held only
because PostgreSQL does not serialize writers. A task still held a transaction and a pooled connection across
every model call, and three concurrency defects surfaced (§Q.4).

### Q.3 Transaction-scope change

Audit of every long wait a request can reach, and its state after this run:

| Wait | Before | After |
|---|---|---|
| Model call (worker step) | inside the request's transaction | committed before (`TaskEnvironment.release_store`) |
| Tool run (incl. Android dispatch) | inside | committed before |
| Memory hydration search | inside | committed before; the membership read uses its own short session |
| Memory formation (extraction call, provider write) | inside | committed before the call and before each `provider.add` |
| Memory API write (`POST /memory`) | inside | committed before `provider.add` |
| Android / confirmation waits | the request ends (task pauses) | unchanged: no transaction |
| Judge | background, short sessions of its own | unchanged: its persist retry sleeps outside any session |
| Scheduler firing, device channel | commit per step already | unchanged |
| Memory API reads; voice server STT/TTS | inside (read-only) | **unchanged** (§N) |

02 §1.2 still holds. Every request-level refusal (token, device, session, membership, capability) happens
before the first commit point, and still commits its audit and nothing else. A task's later decisions are
committed at each point together with their audit. They are never split from it.

### Q.4 Concurrency correctness on real PostgreSQL

`tests/runtime/test_concurrent_store.py`, written first. On the Slice B commit (PostgreSQL, before §Q.3) three
of its tests failed for exactly the intended reasons, and all pass after:

| Test | Before §Q.3 (PostgreSQL) | After |
|---|---|---|
| no transaction open (`pg_stat_activity` "idle in transaction" = 0) during a model call, a second model call and a tool run | **failed**: open at all three | pass |
| a same-key retry from the user's other device while the original runs | **failed**: the task ran twice; the second insert hit the key's primary key → `500` | pass: `409 {"idempotency":"in_progress"}`, then replay of the one result; 1 task row, 1 model call |
| two users' paid calls against a global budget with room for one | **failed**: 2 paid calls | pass: 1 call, spend ≤ limit, the other `429 budget_exceeded` |
| ten users' tasks at once: rows consistent (status, counters, response, owner, one usage row each) | pass | pass |
| a real PostgreSQL deadlock (40P01) → retryable `503`, store not named | pass | pass |

Also on PostgreSQL: the pilot-concurrency suite (10 users; private facts, hydration, flood), MEM-T1, the
lifecycle and rollback tests of the runtime suite, and the whole server suite. No lost update, duplicate,
deadlock or lock timeout appeared in any run. The ten-user row check and the deadlock probe look for exactly
those.

### Q.5 Migrations and CI

- PostgreSQL 16 and SQLite: `alembic upgrade head → downgrade base → upgrade head → check`: clean, head
  `a2d6e8f4c0b9`, no drift. Migration history was not rewritten: one additive revision, plus the initial
  migration's check constraint expressed as `NOT is_authorization_signal` (same meaning on SQLite).
- CI job `postgres` (`.github/workflows/ci.yml`): a `postgres:16` service container with trust authentication
  on the job network, so there is no credential anywhere. A reachability step fails, never skips. Then the
  migration round-trip on PostgreSQL, the server suite, the memory suite and PRD #32 as its own step. The SQLite
  job deselects PRD #32 by name. First run: CI run 86 on `675daae`, every job green, the `postgres` job's
  PRD #32 step included (it asserts every S1/S2 condition).

### Q.6 Regression, security and secret audit

- Suites on both stores, contracts and BR-T2: §C.
- Guard mutations: 44 mutants: the 41 of §E plus **M42** (idempotency in-flight guard removed), **M43**
  (admissions not counted), **M44** (store not released before a long wait), run on PostgreSQL. Result:
  **44/44 killed** (the Kotlin device-guard mutants included, via Gradle).
- Locked contracts: no change to authorization, identity, capabilities, confirmation, memory isolation, Judge
  authority or Android authority. The two `flush()` calls only order inserts. BR-T2 is row-for-row identical.
- Secrets: no database credential in git, fixtures, logs, the APK or the dashboard. CI uses trust auth; local
  runs used a trust-auth throwaway cluster. The only credential-shaped URL added to git is the config test's
  refusal case, marked `TEST-ONLY` (`TESTONLYpw`). Scan of every line added since `08119ac`: 0 other hits.
  Live checks: a password in `database_url` and in `HYPERMIND_DATABASE_URL` is refused without echoing it. A
  marker in `PGPASSWORD` did not appear in the server's startup-failure log.

### Q.7 PRD #32 final status

**PRD #32: MET on PostgreSQL** (§I). This clears §P blocker 2 and nothing else. **The Real-Data Gate stays
CLOSED.**
