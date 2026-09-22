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

---

## 4. Dimensions still PENDING — not measured, not claimed

14 §4's experiment covers "user B's memory, files, and secrets". Two of those
three cannot be measured yet, because the subsystems do not exist:

| Dimension | Status | Why |
|---|---|---|
| Relational store (users, devices, graphs, files-as-rows, capability grants, secrets) | **MEASURED** — §3 above | `security-core` owns these tables |
| **Mem0 / memory store** (`11`) | **PENDING** | No Mem0 integration exists in any branch yet. MEM-T1's cross-user query filter cannot be attacked before it is written. |
| **Filesystem sandbox** (`09`) | **PENDING** | No sandbox roots, no path resolution, no `FileResource` content on disk. FS-T5/FS-T9 are `09`'s to measure. |
| Network egress exfiltration (`10`) | **PENDING** | No egress enforcement exists; NET-T8 is `10`'s to measure. |

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
| Mem0 / filesystem cross-user reach under RCE | Not yet measurable (§4) | — | Re-run BR-T2 after `09`/`11` |

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

- Re-run BR-T2 once `09` and `11` exist, and add their rows to §3 (§4 above).

---

## 7. How to re-run

```bash
python3 -m pytest tests/security_core/test_od_a1_br_t2.py -q -s
```

`-s` prints the measured table. The experiment asserts the measurement in **both**
directions: the contained rows must stay contained, and the reachable rows must
stay reachable. A failure on a *reachable* row means a boundary genuinely
improved — in which case BR-T2 must be re-run and this document updated, rather
than the assertion being deleted.
