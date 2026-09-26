# OD-A1 / BR-T2 — measured blast radius under application-level RCE

**Status:** **RESOLVED FOR PILOT — ACCEPTED RESIDUAL** (owner decision, option (a), 2026-09-22)
**Previously:** `[OPEN — OWNER]` — superseded by the decision recorded in §6 and `docs/DECISION_REGISTER.md`
**Experiment:** `tests/security_core/test_od_a1_br_t2.py`
**Authority:** `14_SECURITY_BLAST_RADIUS.md` §4, `17_TEST_ACCEPTANCE_VALIDATION.md` §4
**Branch that produced this measurement:** `security-core` (re-verified unchanged by `runtime`)

> Accepting the residual is **not** a claim of isolation. The rows marked
> REACHABLE below are exactly what the owner accepted, and the experiment keeps
> asserting they stay reachable, so this document cannot silently become a false
> "isolated" claim (INV-20).

---

## 0. What this document is, and what it is not

14 §4 asks for one thing, and is unusually explicit that it is not a verdict:

> **Deliverable:** the **measured blast radius** — exactly what was and wasn't
> reachable — not a pass/fail. […] Documenting the real reachable set honestly is
> the deliverable.

So this is a measurement, not a claim of success. It exists so the owner can make
the OD-A1 decision — (a), (b) or (c) in §4 below — against observed facts rather
than an assumption.

**This document does not claim that cross-user isolation holds under
application-level RCE.** Per INV-20, the package explicitly does not claim it, and
§3 below records precisely where it does not hold.

---

## 1. The question

> On a single laptop running one FastAPI process with shared memory backends, if
> an attacker achieves application-level RCE (SEC-H/SEC-I), can they read
> *another* user's data — memory/files/secrets — despite the logical isolation
> (`04` visibility filters, `11` query filters, `09` sandbox roots, `12`
> handle-only)? — 14 §4

## 2. Attacker model as measured

Code executing **inside** the FastAPI process, with the ordinary Python access
that implies: the live database session, the constructed `SecurityCore`, and
module globals. Acting as user A's compromised process, attempting to reach user
B's data.

Explicitly **not** modelled: a remote API client (covered by the ordinary
authorization suite), root on the host, or physical access.

---

## 3. Measured results

Re-measured on every test run. `contained` means the attempt failed; `REACHABLE`
means it succeeded.

| # | Attempt | Result | Why |
|---|---|---|---|
| 1 | Read B's private file **through the authorization engine** | contained | The engine denies with `404` (AZ-T1). The logical boundary holds for every caller that uses it — which is every legitimate caller. |
| 2 | Read B's private rows by **querying the shared store directly** | **REACHABLE** | The visibility filter is application code. An attacker who *is* the application is not obliged to call it. This is 14 §4's core admission. |
| 3 | Resolve B's secret by **constructing a requester claiming to be B** | **REACHABLE** | `SecretRequester` is a value the caller supplies. Mediation binds the *claim*; in-process code chooses its own claims. |
| 4 | Resolve any secret through the **agent** requester | contained | Unconditional denial (SECRET-002/INV-6). No scope or class overrides it. |
| 5 | Resolve a `class=master_key` reference **without superuser authority** | contained | Denied for every non-superuser requester (SS-T4, SUPER-001). |
| 6 | **Extract the unlocked DEK** from the live store object and decrypt any secret | **REACHABLE** | 12 §4's stated residual, verbatim: "a compromised *running* process reading in-memory unlocked secrets is a real limit". |
| 7 | Decrypt the store from **database contents alone**, without process memory | contained | The KEK is external. A stolen DB or backup yields no plaintext (SS-T3, BR-T3). |
| 8 | Recover the **KEK** from the database | contained | Only the wrapped DEK is stored; the KEK is never written (12 §3). |
| 9 | **Forge a device-credential proof** using server-side material | contained | The server stores only public verifiers. No private key exists server-side to sign with — the payoff of 03 §4.2's `[REC]` asymmetric choice. |
| 10 | **Self-grant an absolute-floor capability** by writing the grant row directly | contained | The row can be written, but authorizes nothing: no registry entry and no tool exposes a floor operation (PERM-006, prohibition by absence). |
| 11 | **Mint superuser authority** from inside the process | **REACHABLE** | `SuperuserGrant` is a process-local object. In-process code can construct one, as `server/secrets/requester.py` states outright. |

### Summary of the measured radius

**Reachable under app-level RCE (4 of 11):** every other user's stored rows; any
secret whose handle is known, via a forged requester or the in-memory DEK; and
superuser authority within the process.

**Contained even under app-level RCE (7 of 11):** the at-rest boundary (a stolen
database or backup is useless without the external KEK); the KEK itself; device
impersonation; master-key resolution through the ordinary requester path; the
agent's access to any secret at all; and floor-capability escalation, which is
prohibited by absence rather than by a check.

The shape of the result: **the at-rest and by-construction boundaries hold; the
in-process logical boundaries do not.** That is exactly what 14 §4 predicted, now
measured rather than assumed.

### 3b. Execution dimensions (integration-hardening re-run)

§4 below required a re-run once the filesystem sandbox (`09`) and egress
boundary (`10`) existed. They do now, and so does a surface the original table
could not see: the `system.restricted` process executor. The re-run lives in
`tests/integration/test_br_t2_execution_rows.py` and runs in CI with `-s`, so the
table is printed on every run and asserted in both directions.

It separates two attacker models, because the owner's OD-A1 (a) acceptance
covers only the first:

* **app-RCE** — code running inside the server process (the accepted class).
* **authorized** — an ordinary user driving the agent through paths the
  deterministic layer *allows*, including a human-confirmed, step-up-fresh
  `system.restricted` call. Anything reachable this way is **not** inside the
  accepted class: it is a cross-user authorization failure and goes to the owner.

| # | Attempt | Model | Result | Why |
|---|---|---|---|---|
| 12 | Read B's sandbox file through A's own `files.read` tool (`..` traversal) | authorized | contained | Roots derive from the server-side user id; `..` and symlinks are refused at every hop (09 §1/§2). |
| 13 | Read B's sandbox file by opening its path in-process | app-RCE | **REACHABLE** | `mediated` containment is application code; the server's OS user owns every root (09 §8, OD-FS-1). Inside OD-A1 (a). |
| 14 | Open a raw socket to an undeclared destination in-process | app-RCE | **REACHABLE** | `mediated_proxy` binds this codebase's adapters, not the process (10 §3, OD-NET-1). Inside OD-A1 (a). |
| 15 | Read B's sandbox file from an approved `system.restricted` command | authorized | contained (Landlock hosts) | Landlock ruleset: reads only system dirs + the task's own temp root. **Before integration-hardening this row was REACHABLE** — the allow-listed program ran with the server's full filesystem view. |
| 16 | Read the server's environment (`env:` KEK, superuser token) from an approved command | authorized | contained (Landlock hosts) | `/proc` is outside the ruleset. **Previously REACHABLE.** |
| 17a | Row 15 with break-glass **enabled** but no record for the task | authorized | contained | Enablement is key one of two; it runs nothing unconfined (20 §2.2). |
| 17b | Row 15 from **another** task while a record is live | authorized | contained | A record binds one task, its owner, and named executables. |
| 17c | Row 15 **inside an active break-glass window** | authorized | **REACHABLE** | The record removes the kernel layer from that one child — OD-A1's residual, reached deliberately and audited (20 §4). |
| 17d | Row 16 inside an active break-glass window | authorized | **REACHABLE** | Same. Every activation on a multi-user server with real data is a cross-user exposure event. |
| 17e | Row 15 after the record's invocations are spent | authorized | contained | The record has ended. |

Row 17 was, until U6, "row 15 under the global `confinement_mode: unconfined`
opt-out" (REACHABLE for every task, for as long as the config said so). That
switch no longer exists (a config naming it fails to load); unconfined
execution is now reachable only as 17c/17d — one superuser-activated,
task-bound, count- and time-limited record (BG-T10). It is recorded here as
reachable, not claimed closed.

On a host without Landlock, rows 15–16 print `NOT MEASURED`: normal
`system.restricted` then refuses to run any process at all (fail-closed), so
there is nothing to measure. The previous 11 rows are unchanged (4 REACHABLE, 7
contained).

What the re-run does **not** change: rows 13 and 14 are the same residual class
as rows 2 and 6 — a compromised live process reaches what that process can
already reach. Confinement narrows what an *authorized* shell command can reach;
it does nothing for code already inside the server. Logical isolation is not
process isolation.

### 3c. Persistent memory (memory build re-run, docs/21 MP-T12)

§4 required a re-run once `11` existed. The self-hosted Mem0 store now exists
(`server/memory/mem0_provider.py`), with the Knowledge Vault beside it. The re-run
lives in `tests/integration/test_br_t2_memory_rows.py`, runs on the **real** Mem0
+ Chroma store in CI with `-s`, and asserts every row in both directions.

It adds a third attacker model the earlier tables did not need: **at-rest** —
someone holding the store's files without the process (a stolen disk or
backup). OD-A1 covers the app-RCE class only; the at-rest rows are reported to
the owner below, not accepted by this document.

| # | Attempt | Model | Result | Why |
|---|---|---|---|---|
| 18 | B lists / searches A's private fact via `GET /memory` | authorized | contained | Visibility in the store query **and** the engine's `readable()` re-check with live membership |
| 19 | B's agent hydrates A's private fact | authorized | contained | Hydrator re-check (MEM-T1 / MP-T1) |
| 20 | B corrects / shares / deletes A's facts (private → 404, shared → 403) | authorized | contained | Engine D3/D4 on the `mem0fact` projection; share and delete also need a bound confirmation |
| 21 | B reaches memory through the vault API | authorized | contained | Distinct client, directory and collection (VAULT-003) |
| 22 | B reads A's graph-shared fact after removal from the graph | authorized | contained | Membership read live on every call |
| 23 | In-process code queries the Mem0 store for A's scope directly | app-RCE | **REACHABLE** | The visibility filter is application code — the same class as row 2. Inside OD-A1 (a). |
| 24 | Resolve a SecretStore secret through the memory/vault packages | app-RCE | contained | No import chain to `server.secrets` (contract); the provider holds no secret handle |
| 25 | Find a secret value inside the memory store | app-RCE | contained | The write gate rejects secret-shaped text before storage (MP-T5) |
| 26 | Recover a **deleted** fact's text from the store files | app-RCE | contained | Mem0's history DB disabled; Chroma's write-ahead log flushed and the SQLite file vacuumed on every destructive call |
| 27 | Read **live** facts from the store files without the process | at-rest | **REACHABLE** | Plaintext at rest; filesystem permissions (0700) only — docs/21 §7 already states this |
| 28 | Recover a **deleted** fact's embedding vector from the store files | at-rest | **REACHABLE** | Chroma's HNSW index marks deleted vectors and keeps their bytes until the index is rebuilt. A vector, not text — but embedding-inversion can approximate text |

No *authorized* row is reachable. Row 23 is inside the accepted class. Rows 27
and 28 are **outside OD-A1's class** (they need no running process) and are listed
in §5 as owner actions; they are one reason the real-data gate stays closed.

---

## 4. Dimensions still PENDING — not measured, not claimed

14 §4's experiment covers "user B's memory, files, and secrets". One of those
three still cannot be measured, because the subsystem does not exist:

| Dimension | Status | Why |
|---|---|---|
| Relational store (users, devices, graphs, files-as-rows, capability grants, secrets) | **MEASURED** — §3 above | `security-core` owns these tables |
| **Mem0 / memory store** (`11`) | **MEASURED** — §3c rows 18–28 | Authorized reach contained; in-process reach REACHABLE (accepted class); at-rest reach REACHABLE (owner action, §5) |
| Filesystem sandbox (`09`) | **MEASURED** — §3b rows 12, 13, 15, 17 | In-process reach is REACHABLE (accepted class); authorized reach is contained |
| Network egress exfiltration (`10`) | **MEASURED** — §3b row 14 (in-process); `system.restricted` sockets denied by Landlock TCP rules + seccomp `socket()` filter | In-process reach is REACHABLE (accepted class) |
| Android device (`08`) | **PENDING** | No device client exists; `UnavailableDeviceTransport` refuses every call |

`[LOCKED]` these rows are **pending, not passing**. They are measured on the same
basis when `09` and `11` exist. The owner's acceptance (§6) covers the *class* of
residual — a compromised live process reaching what that process can already
reach — so a pending row that turns out REACHABLE for that reason falls inside
the accepted risk. A row reachable for any *other* reason goes back to the owner.

The `runtime` branch adds no new in-process store: the agent's working transcript
is volatile, and `agent_tasks` holds only owner-scoped lifecycle rows. Those rows
are reachable under RCE on the same basis as row 2, and nothing about them changes
the measured radius.

---

## 5. Residual risks and owner actions (BR-T4)

14 §5's residual table, restricted to residuals this branch's scope can speak to:

| Residual | Why it remains | Mitigation in place | Owner action |
|---|---|---|---|
| App RCE reads co-tenant rows and **in-memory** unlocked secrets | Single-process pilot; logical boundaries are application code (rows 2, 3, 6 above) | Logical isolation for all legitimate callers; at-rest encryption with external KEK; handle-only agent access | **Accepted for pilot — option (a)** (§6) |
| In-process superuser minting | `SuperuserGrant` is process-local (row 11) | The credential itself is a separate env var from the KEK, so neither yields the other to an attacker who only reads config | **Accepted for pilot — option (a)** |
| **Stolen device credential** before revocation | "Logged in until revoked" is the deliberate UX choice (SESSION-001, 03 §7) | Short access-token TTL (15 min); step-up on credential rotation; immediate revocation killing live tokens | Accept, or shorten TTL |
| Stolen access token | Short TTL | Opaque tokens with server lookup → revocation is immediate, not TTL-bounded | Accept |
| Filesystem / egress reach under **app** RCE | `mediated` fs and `mediated_proxy` egress are application code (§3b rows 13, 14) | Authorized paths contained; `system.restricted` kernel-confined by default (§3b rows 15, 16) | **Accepted for pilot — option (a)**; `mount_isolated`/`netns_filtered` remain future hardening |
| `system.restricted` under an active break-glass record | Owner-ratified OD-EXEC-2: deliberate, for recovery (§3b rows 17c/17d) | Off by default; superuser-only, per task, per executable, ≤ `max_invocations`, ≤ 15 min and never past the task; every activation/invocation/end audited; still needs confirmation + step-up | Treat each activation with real data as a cross-user exposure event; transcribe OD-EXEC-2 into `DECISION_REGISTER.md` §2B (OD-BG-1) |
| Mem0 cross-user reach under **app** RCE | The visibility filter is application code (§3c row 23) | Authorized paths contained (rows 18–22); secrets never stored (row 25); deleted text physically removed (row 26) | Inside **option (a)** |
| Memory store **at rest**: live facts in plaintext, deleted facts' vectors retained (§3c rows 27, 28) | No at-rest encryption for memory yet (docs/21 §7, DECISION_REGISTER §4 "future hardening"); hnswlib marks rather than erases | Owner-only (0700) store directories; disk/backup protection is the operator's | **Owner decision needed before real data**: accept for the pilot, or require encrypted storage / a periodic index rebuild first |

Every residual above is documented with an owner action, and none is presented as
solved.

---

## 6. The gate `[LOCKED]`

From 14 §4 and PILOT-004:

> **no real, non-disposable user data is entrusted to the pilot until OD-A1 is
> reviewed and either accepted for the pilot's risk level or upgraded to
> (b)/(c)**

**Current position: OD-A1 is decided — option (a), accepted for the pilot's risk
level.** The owner reviewed this measurement and explicitly accepted the residual
in §3 and §5. The pilot may proceed under that accepted risk model.

What this does **not** do:

- It does not claim isolation under application RCE. Rows 2, 3, 6 and 11 are
  still reachable, and that is what was accepted.
- It does not relax cross-user *logical* isolation, which stays mandatory and
  tested (AZ-T1, AZ-T10, the runtime's confused-deputy tests).
- It does not by itself make the build **real-user-ready**. 17 §5 requires the
  whole release-blocking set to be green as well, and that set includes the `09`,
  `10`, `11` and `08` suites whose subsystems do not exist yet. Until they do, the
  pilot runs on **disposable or test data**, for that reason rather than OD-A1's.

### The owner's options (14 §4)

| Option | What it buys | Cost |
|---|---|---|
| **(a) Accept, document, gate** | Pilot proceeds with logical isolation plus a written limit: real user data only after review; disposable/test data until then | Cheapest; honest; leaves the residual for the pilot |
| **(b) Per-user process isolation** | Each user's agent/tool/session in a separate process or container → RCE in one does not trivially read another's memory | More infra; still a shared DB unless (c) |
| **(c) Per-user data-store isolation** | Separate DB/collection/encryption context per user → app RCE cannot read another's store without that user's key | Most isolation; most complexity for a student-scale pilot |

14 §4 recommended **(a)**. This measurement showed it was *viable* — rows 7–10
show the at-rest and by-construction boundaries are real — and that rows 2, 3, 6
and 11 are its price. **The owner chose (a)** and accepted that price for the
pilot.

**What would reopen it:** a deployment whose trust model changes — hostile or
multi-tenant hosting, or a pilot population that should not trust the operator's
process with each other's data. Then (b) or (c) is the upgrade path. Both are
recorded as future hardening in `docs/DECISION_REGISTER.md` §4.

### Still to do (not blocking OD-A1)

- ~~Re-run BR-T2 once `09` exists~~ — done in integration-hardening (§3b). Rows 13
  and 14 fall inside the accepted class; no *authorized* path was found reachable
  with the default `landlock` confinement.
- ~~Re-run BR-T2 once `11` (Mem0) exists~~ — done in the memory build (§3c). No
  authorized path reachable; row 23 inside the accepted class; rows 27–28 are
  at-rest and go to the owner (§5).
- Re-run BR-T2 once `08`'s device client exists, and add its rows.

---

## 7. How to re-run

```bash
python3 -m pytest tests/security_core/test_od_a1_br_t2.py tests/integration/test_br_t2_execution_rows.py \
    tests/integration/test_br_t2_memory_rows.py -q -s
```

`-s` prints the measured table. The experiment asserts the measurement in **both**
directions: the contained rows must stay contained, and the reachable rows must
stay reachable. A failure on a *reachable* row means a boundary genuinely
improved — in which case BR-T2 must be re-run and this document updated, rather
than the assertion being deleted.
