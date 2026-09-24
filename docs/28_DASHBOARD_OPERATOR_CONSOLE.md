# 28_DASHBOARD_OPERATOR_CONSOLE.md
## JARVIS / Hypermind Track B — Dashboard & Operator Console

**Package:** next-build subsystem contract · **Depth:** compact · **Written:** 2026-09-24
**Numbering:** slot `28` is already cited for the dashboard by `02` §12, `13` §7, `14` (INV-16/17) and `16` §5. This document fills it.
**Status:** formalizes the owner's **dashboard decision** (owner-ratified 2026-09-24): an operational/control/observability surface, not an intelligence authority, not another agent; respects authorization, graph boundaries, secret redaction, PII rules.
**Authority:** below `00_CANONICAL_PRD.md` §28 (`[LOCKED]` DASH-001..006) and `docs/DECISION_REGISTER.md`. Endpoints `GET /api/v1/admin/*` already specified in `02` §12. Import contract "Dashboard cannot import secrets (DASH-002)" already enforced in `pyproject.toml`.
**Code this plugs into:** `server/dashboard/` (placeholder), `server/security/superuser.py`, the audit and usage ledgers, `18` (breaker/recovery state and control endpoints), `19` (evaluations), `20` (break-glass state), `21` (memory/vault stats).

---

## 0. The rule this document exists to enforce

> **The dashboard shows. Controls live elsewhere.**

---

## 1. The tension this document resolves

The owner calls the dashboard a "control surface" exposing configuration and recovery state. PRD §28 locks **DASH-002: the dashboard is read-only over gated results; it exposes no mutation path.** A subsystem document cannot override a `[LOCKED]` PRD clause.

`[PROPOSED]` resolution that satisfies both:

- `server/dashboard/` stays **read-only** — every route is `GET`, and it imports no mutation path (DASH-002 holds at the module level, enforceable by import-linter).
- Operator **controls** (stop, global stop/clear, break-glass activation, Judge enable/disable, improvement-candidate approval) are superuser endpoints under `/api/v1/admin/control/*`, owned by the modules that enforce them (`18` breaker, `20` break-glass, `19` review queue) — not by the dashboard.
- The operator **console UI** may present both side by side. Viewing uses dashboard routes; acting calls control routes. The user experiences one console; the code keeps observation and administration separate.

`[OPEN — OWNER]` OD-DASH-1: ratify this split, or amend DASH-002 in the PRD. Recommended: ratify the split — it is what DASH-002 was protecting.

---

## 2. Who sees what

| Surface | Audience | Authorization | Content |
|---|---|---|---|
| Operator console (`/admin/*` views + `/admin/control/*`) | the operator | superuser principal only (`[LOCKED]` DASH-003/004) | all tenants, **secret-free and PII-redacted by default** (DASH-005/006) |
| User activity views | each user, about themselves | ordinary Bearer token, `04` authorization | the user's own tasks, jobs, memories, grants, devices, evaluations — through the existing user API (`02`), in the Android app |

`[PROPOSED]` "the dashboard respects graph boundaries" is satisfied by the second row: users see their own and graph-shared data through the normal authorized API. The operator console is not a user surface and is never reachable with a user token.

`[LOCKED]` viewing unredacted user content in the console is a separate, privileged, audited action (DASH-006).

---

## 3. Views (read-only)

| View | Shows | Source |
|---|---|---|
| Health | component status: DB, SecretStore locked/unlocked, model providers, Mem0, vault index, scheduler, device channel | health probes |
| Tasks | running/paused/terminal counts; per-task status, mode, counters, failure codes, worker switches | `agent_tasks` + audit |
| Recovery & breaker | switches, stalls, trips by source, global latch state | `18` audit events |
| Break-glass | enablement banner; active records with task, reason, expiry, invocations | `20` |
| Evaluations | Judge scores and findings, **redacted**; improvement-candidate queue | `19` |
| Usage & budget | per-user and global spend and rate usage, including Judge spend | usage ledger (`13`) |
| Memory & vault | fact counts per user (no content by default); vault documents, last reindex, index errors | `21` |
| Devices | registered devices, connected state, last seen, revoked | `03`, `23` |
| Audit | filterable audit log; resource ids and results, never values | audit ledger |
| Configuration | effective configuration with every secret shown only as a handle and whether it resolves | config + SecretStore metadata (`[LOCKED]` DASH-005) |

---

## 4. Implementation notes

- `[LOCKED]` same gateway process, routers under `/api/v1/admin/*` (SRV-002; PRD §6 "separate internal-facing surface on the same Gateway"). PRD §3 goal 5 names a "Flask/operator dashboard"; `[PROPOSED]` the console UI may be a static page served by the gateway calling these routes. No second server process is required.
- `[PROPOSED]` redaction is applied server-side before the response leaves the route; the UI never receives unredacted content it is supposed to hide.
- `[PROPOSED]` the console shows **one** persistent banner whenever break-glass is enabled or a global stop is latched.

---

## 5. Open items

| ID | Question | Status |
|---|---|---|
| OD-DASH-1 | Read-only dashboard + separate control endpoints (§1) vs amending DASH-002 | `[OPEN — OWNER]`, rec ratify split |
| OD-DASH-2 | Console UI technology | `[IMPL]` |

---

## 6. Acceptance hooks (`[PROPOSED]` IDs, plus existing API-T8 / PRD #29)

- **DSH-T1** every `server/dashboard` route is `GET`; the module imports no mutation or secret-resolution path (CI).
- **DSH-T2** an ordinary user token cannot reach any `/admin/*` or `/admin/control/*` route (DASH-004).
- **DSH-T3** no view contains a secret value; configuration shows handles and resolvability only (DASH-005).
- **DSH-T4** user content is redacted by default; unredacted viewing is audited (DASH-006).
- **DSH-T5** break-glass enablement and a latched global stop show a persistent banner.

---

*End of 28. See `NEXT_BUILD_INDEX.md`.*
