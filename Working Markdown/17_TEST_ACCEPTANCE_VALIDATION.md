# 17_TEST_ACCEPTANCE_VALIDATION.md
## Hypermind Track B — Test / Acceptance / Validation

**Package:** subsystem doc 17 of 17 (final) · **Depth:** deep · **Status:** validation contract & definition-of-done
**Authority:** subordinate to `00_CANONICAL_PRD` (§39 acceptance criteria, §44 threat matrix). Consolidates the acceptance hooks defined at the end of `01`–`16` into one executable contract. Nothing here is claimed as tested — this defines *what must be tested and what "pass" means.*
**Consumed by:** the implementer (build-to-green), the owner (release gate).

**Label legend:** `[LOCKED]` · `[IMPL]` · `[FUTURE]` · `[OPEN — OWNER]`.

---

## 0. The rule this document exists to enforce

> **A requirement is "met" only when its test passes with recorded evidence — not when the code exists.** Every guarantee the package makes maps to at least one test here; the release-blocking subset must be green before real user data touches the system.

---

## 1. Test taxonomy

| Category | Purpose | Examples |
|---|---|---|
| **Schema/unit** | contracts hold in isolation | DM-T1..T9 (`01`) |
| **Integration** | components compose correctly | request lifecycle (`02`), authz across modules (`04`) |
| **Isolation** | a boundary can't be bypassed even by a bypassing actor | FS-T9 (`09`), NET-T2 (`10`), SS-T3 (`12`) |
| **Adversarial** | attack experiments | SEC-A..V (`14`) |
| **Failure/recovery** | explicit failure, fail-closed, no fabricated success | FAIL-* (`02`§13), FI-analogues |
| **Concurrency/multi-tenant** | cross-user boundaries under real multi-user load | AZ-T1/MEM-T1 (`04`/`11`), per-principal limits (`13`) |
| **Self-host** | a fresh clone runs on the cloner's own credentials | CFG-T1 (`15`) |
| **Boundary-lint** | module dependency rules enforced in CI | REPO-T1..T7 (`16`) |

---

## 2. Consolidated acceptance matrix (requirement → test → evidence → pass)

Each row: the source doc's hook ID, what it verifies, the fixture/setup, the expected result = pass condition, and whether it is **release-blocking (RB)**. (Full per-test setup lives in the cited doc; this is the master index.)

### Identity / Auth / Session (`03`)
| Test | Verifies | Pass = | RB |
|---|---|---|---|
| AUTH-T1 | tampered id_token rejected | 401, no user created | RB |
| AUTH-T3 | email change keeps same user (subject-keyed) | same user_id | RB |
| AUTH-T4 | device credential returned once, never re-exposed | not in any later response/log | RB |
| AUTH-T5 | revoked device can't refresh | refresh fails | RB |
| AUTH-T7 | body-asserted user_id ignored; token identity used | served as token's user | RB |
| AUTH-T10 | Session.user_id == Device.user_id enforced | cross-user session impossible | RB |

### Authorization / Graph / Resource (`04`) — the leak-prevention surface
| Test | Verifies | Pass = | RB |
|---|---|---|---|
| **AZ-T1** | member can't read another's private resource in shared graph | 404 | **RB (top)** |
| AZ-T2 | resources default private | visibility=private | RB |
| AZ-T3 | non-member request → 404 (anti-enum) | indistinguishable from absent | RB |
| AZ-T5 | share on secret-class refused | 403 prohibited | RB |
| AZ-T7 | capability ≠ visibility bypass | can't read another's private file with file.read | RB |
| AZ-T9 | any authz error → deny (fail-closed) | never allow | RB |
| AZ-T10 | agent for A can't read B's private data | confused-deputy blocked | RB |

### Runtime / Model / Tools / Device (`05`/`06`/`07`/`08`)
| Test | Verifies | Pass = | RB |
|---|---|---|---|
| RT-T1 | unauthorized proposal denied, not bypassed | denial fed back | RB |
| RT-T2 | bounds breach → explicit stop | no silent/fabricated finish | RB |
| RT-T3 | consequential action → confirm, no timeout-approve | not executed without confirm | RB |
| RT-T4 | absolute-floor → prohibited, never confirmable | prohibited | RB |
| RT-T6 | model outage → explicit failure | no fabricated answer | RB |
| MP-T2 | API key never in config/messages/logs/usage | absent everywhere | RB |
| TL-T3 | capability doesn't bypass visibility | denied | RB |
| TL-T5 | absolute-floor op prohibited; no grant creatable | prohibited | RB |
| TL-T6 | MCP gets no trust from conformance | capability-gated+bounded | RB |
| AND-T1 | device capability = enumerated ops only | out-of-mapping op unexecutable | RB |
| AND-T4 | consequential device action needs confirm | not auto-executed | RB |
| AND-T5 | user-toggled-off op rejected device-side | rejected even if server-forwarded | RB |

### Filesystem / Network (`09`/`10`) — DEEPEST isolation
| Test | Verifies | Pass = | RB |
|---|---|---|---|
| FS-T1 | traversal rejected | `../../etc` refused | RB |
| FS-T2/T3 | symlink-escape / zip-slip refused | rejected | RB |
| FS-T5 | user A can't reach B's sandbox | blocked | RB |
| FS-T6 | private file in shared graph unreadable by others | blocked | RB |
| **FS-T9** | compromised tool ignoring path helper still can't escape | contained | **RB (real containment)** |
| **NET-T2** | compromised tool can't reach non-declared dest via any mechanism | dropped | **RB (real containment)** |
| NET-T3 | metadata IP unreachable even internet-capable tool | blocked | RB |
| NET-T8 | tool can't exfiltrate secret / open reverse shell | no allowed dest | RB |

### Memory / Secrets / Usage (`11`/`12`/`13`)
| Test | Verifies | Pass = | RB |
|---|---|---|---|
| **MEM-T1** | B can't retrieve A's private fact via query/hydration/agent | filtered out | **RB (memory leak)** |
| MEM-T3 | non-{pref,past_request,goal} fact_type rejected | rejected | RB |
| MEM-T6 | mem0/vault distinct collections+clients | distinct | RB |
| **SS-T1** | secret value never in git/APK/logs/dashboard/usage/agent/mem0 | absent | **RB (leak)** |
| **SS-T2** | agent can only reference by handle, can't call get | can't resolve | **RB** |
| **SS-T3** | DB/backup leak without KEK → no plaintext | useless | **RB** |
| SS-T4 | master-key ref unresolvable by agent/tool | denied | RB |
| SS-T8 | store locked → fail closed, no raw fallback | operation denied | RB |
| US-T1 | every model/tool call metered | one UsageEvent each | RB |
| US-T3 | budget-breaching paid call refused | explicit, not made | RB |

### Security / Blast-radius (`14`)
| Test | Verifies | Pass = | RB |
|---|---|---|---|
| SEC-A..V | each threat's expected block holds | boundary holds | RB (behavioral) |
| INV-1..19 | each invariant's test passes | holds | RB |
| INV-20 | cross-user isolation under RCE NOT falsely claimed | §4 genuinely open | RB (honesty) |
| **BR-T2** | OD-A1 RCE experiment run + blast radius documented | measured, no false "isolated" claim | **RB (go/no-go for real data)** |
| BR-T4 | residual table present, each has owner action | documented | RB |

### Config / Repo (`15`/`16`)
| Test | Verifies | Pass = | RB |
|---|---|---|---|
| CFG-T1 | fresh clone runs on cloner's own creds | no owner secrets needed | RB |
| CFG-T3 | no secret literal in repo/templates | absent | RB |
| CFG-T4 | mem0≠vault collection enforced at load | same-value fails start | RB |
| CFG-T5 | intelligence disabled, system fully functional | works | RB |
| REPO-T1 | agent can't import raw-secret resolution | CI fails on violation | RB |
| REPO-T2 | no server secret in android/APK | absent | RB |
| REPO-T7 | dependency rules enforced in CI | deliberate violation fails build | RB |

---

## 3. Mapping to the PRD's 32 acceptance criteria (`00` §39)

`[LOCKED]` Every one of the PRD's 32 MVP acceptance items (§39 #1–#32) maps to ≥1 test above. Coverage confirmed for the security-critical ones: #6/#22 private-graph & memory isolation → AZ-T1/MEM-T1; #8 server-authorized membership → AZ-T3/AUTH-T7; #16 dangerous ops confirm/impossible → RT-T3/RT-T4/TL-T5; #19 secrets not leaked → SS-T1/MP-T2; #20 no superuser exposure → SS-T4; #24 intelligence-disabled works → CFG-T5; #26 speaker-ID-not-auth → INV-14; #28 self-host → CFG-T1; #31 adversarial tests pass → SEC-A..V; #32 ~10-device pilot → concurrency/multi-tenant suite. The remaining functional items (#1–5, #10–15, #23, #25, #27, #30) map to the integration/self-host/voice/lifecycle tests in their source docs.

---

## 4. The OD-A1 go/no-go experiment (BR-T2) — the real-data gate

`[LOCKED]` Before any **real, non-disposable** user data is entrusted to the pilot, **BR-T2 must be run and its result reviewed** (`14` §4, PRD PILOT-004):
- **Setup:** a test harness that executes attacker-controlled code inside the application process (simulated app-RCE), acting as user A's compromised process.
- **Attempt:** read user B's memory, files, and (in-memory/at-rest) secrets.
- **Deliverable:** the **measured blast radius** — exactly what was and wasn't reachable — not a pass/fail.
- **Gate:** the owner reviews the measured radius and chooses OD-A1 option (a) accept-for-pilot-with-disposable-data, (b) per-user process isolation, or (c) per-user store isolation. Real user data proceeds only after this decision. **This is the single most important go/no-go in the package.**

---

## 5. Release-blocking set (definition of "safe to run with real data")

`[LOCKED]` The system is safe to run with real user data only when **all RB tests above pass** AND the OD-A1 gate (§4) is decided. The RB set concentrates on the four leak/containment surfaces:
1. **Cross-user data isolation** (AZ-T1, MEM-T1, FS-T5/T6) — no user reads another's private data.
2. **Secret containment** (SS-T1/T2/T3, MP-T2, REPO-T1/T2) — no secret leaks to agent/logs/client/DB.
3. **Boundary non-bypass** (FS-T9, NET-T2/T3/T8) — a compromised tool is contained.
4. **Agent containment** (RT-T1/T3/T4, TL-T5, AZ-T10) — the agent can't act beyond its principal or self-escalate.

A functional-but-not-RB-green build may be *demoed on disposable data*; it is **not** cleared for real users.

---

## 6. Test data & fixtures (`[IMPL, constrained]`)
- Multi-user fixtures: ≥2 users, ≥1 shared graph, private + shared resources per user — the substrate for every isolation test.
- Adversarial fixtures: injection strings (in input, tool output, file content, packet-analogue), traversal/symlink/zip-slip payloads, oversized inputs, malformed tokens.
- `[LOCKED]` No fixture contains a real secret; test secrets are clearly-marked test values.

---

## 7. Open items

| ID | Question | Status |
|---|---|---|
| OD-TEST-1 | test framework/harness choice | `[IMPL]` |
| OD-A1 (PRD) | resolved via BR-T2 + owner decision (§4) | **gates real data** |
| OD-TEST-2 | how "≥10-device pilot" is load-simulated | `[IMPL]`; multi-tenant boundary-under-load required |

---

## 8. Acceptance hooks (meta)

- **T-META-1** every guarantee in `01`–`16` has ≥1 test here (traceability complete).
- **T-META-2** the RB set (§5) is green before real-data release.
- **T-META-3** BR-T2 (OD-A1) is run and reviewed before real data (§4).
- **T-META-4** "met" is defined as test-passes-with-evidence, never code-exists (§0).

---

*End of 17_TEST_ACCEPTANCE_VALIDATION — final document of the Track B package. Definition of done: the package is implementation-ready; a build is real-user-ready only when §5's release-blocking set is green and the OD-A1 gate (§4) is decided.*
