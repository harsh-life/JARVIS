# 22_SCHEDULER.md
## JARVIS / Hypermind Track B — Scheduler (Task-Linked Reminders)

**Package:** next-build subsystem contract · **Depth:** compact · **Written:** 2026-09-24
**Numbering:** slot `22` is already cited as "the scheduler" by `13` (limits, FAIL-010) and `14` (SEC-T). This document fills it.
**Status:** formalizes the owner's **scheduler decision** (owner-ratified 2026-09-24): a task-linked JARVIS runtime subsystem, authorization-aware, capability-aware, never an alternate privileged execution path, implementation-replaceable.
**Authority:** below `00_CANONICAL_PRD.md` §22 (`[LOCKED]` SCHED-001: task-linked reminders only; no unprompted proactivity) and `docs/DECISION_REGISTER.md`. Entity and endpoints already exist in `01` §6.2 (`ScheduledJob`) and `02` §9 (`/api/v1/jobs`); this document adds only runtime behaviour.
**Code this plugs into:** `server/scheduler/` (placeholder today), `server/security/usage.py` (quota), `server/capabilities/registry.py` (the `scheduler.create` entry proposed in matrix §3.2), the device channel in `23` §4 for delivery, task modes in `18` §3.

---

## 0. The rule this document exists to enforce

> **A firing reminder delivers a message. It never executes.** Anything that happens after a reminder happens because the user started a task.

This keeps the scheduler out of the execution path entirely, which is what makes it safe to build now. It is also the only reading consistent with PRD §22 ("task-linked reminders only") and OD-F1 ("suggest must not execute").

---

## 1. Creation

Two entry points, one rule set:

| Entry point | Who | Authorization |
|---|---|---|
| `POST /api/v1/jobs` (`02` §9) | the user, directly | Bearer; server-derived principal |
| worker proposes `scheduler.create.create_reminder` inside an `execute` task | the agent, for the user | `[PROPOSED]` capability `scheduler.create`, tier `low_write` (matrix §3.2), activated like any capability; runs automatically inside a user-instructed task |

Rules:
- `[LOCKED]` `task_reason` required and non-empty — the user's own words, not the worker's paraphrase. `[PROPOSED]` when created by the agent, `task_reason` must be traceable to the task's user input (the runtime copies it from the instruction; the worker cannot author it freely).
- `[LOCKED]` counts against the per-user scheduler quota; breach → `429`, explicit (FAIL-010).
- `[LOCKED]` visibility triplet; default `private`.
- `[PROPOSED]` a job records `origin_task_id` (nullable for direct API creation) and the creating `device_id`.
- `[PROPOSED]` `draft`/`suggest`/`observe` tasks cannot create jobs (`low_write` exceeds their mode ceiling, `18` §3).

---

## 2. Firing

```
job due → scheduler backend fires
  → re-check (deterministic): user active? job still active? if graph-scoped, owner still an active member?
      any "no" → job marked failed/cancelled, audited, nothing delivered
  → deliver a reminder notification to the OWNER's own active devices (23 §4)
  → optional action on the notification: "start task" → opens the app with the task_reason pre-filled
      → user taps → a normal execute task, authorized like any other
  → audit scheduler.job.fired / delivered / undeliverable
```

Rules:
- `[PROPOSED]` there is **no principal at fire time** — no session is live. The scheduler therefore never calls the runtime, never activates a capability, never touches a tool. That is the structural reason it cannot become a privileged path.
- `[PROPOSED]` notification content is the owner's private data; it is delivered only to that owner's devices, never to other graph members, even for graph-scoped jobs.
- `[PROPOSED]` the notification payload sent via any third-party push service carries no content — only a "sync now" signal; content travels over JARVIS's own authenticated channel (`23` §4).
- `[OPTIONAL]` a firing reminder may create a `suggest`-mode task that prepares suggestions for the user to review. It is capped at `low_read` and needs a principal; `[OPEN — OWNER]` OD-SCH-2 decides whether a fire-time principal (the job owner, no session, read-only) is acceptable.

---

## 3. Durability and misfires

- `[IMPL]` backend: APScheduler with a persistent job store in the application database (PRD §22 "APScheduler-class"), behind a small `SchedulerBackend` interface (`add`, `remove`, `list_due`, `mark`) so it can be replaced.
- `[PROPOSED]` jobs survive restarts. Misfires (server down at the due time) deliver late with a "missed at …" note within `misfire_grace`; older misfires are marked `missed` and reported, never silently dropped.
- `[PROPOSED]` a device that is offline receives pending reminders on reconnect (reminders are safe to queue because they carry no authority — unlike device operations, which are never queued, `23` §4).

---

## 4. Configuration (`[PROPOSED]`)

```yaml
scheduler:
  enabled: true
  backend: apscheduler_db
  misfire_grace_minutes: 60
  max_active_jobs_per_user: 50
security:
  rate_limits:
    scheduler_creations_per_hour: 20     # joins the existing rate_limits section
```

---

## 5. Open items

| ID | Question | Status |
|---|---|---|
| OD-SCH-1 | Ratify `scheduler.create` capability name and `low_write` tier (matrix §3.2) | `[OPEN — OWNER]` |
| OD-SCH-2 | Fire-time `suggest` task with a sessionless, read-only principal | `[OPEN — OWNER]`, rec not in next build |
| OD-SCH-3 | Scheduled *execution* (a job that runs actions unattended) | `[FUTURE]`; would need a standing-delegation model and conflicts with PRD §22 as written |

---

## 6. Acceptance hooks (`[PROPOSED]` IDs)

- **SCH-T1** empty `task_reason` rejected via API and via the agent path (DM-T4, PRD #30).
- **SCH-T2** a firing job never invokes the runtime, a capability, or a tool (static + runtime test).
- **SCH-T3** fire-time re-check: a suspended user or revoked graph membership → nothing delivered.
- **SCH-T4** reminders reach only the owner's own devices; push payloads carry no content.
- **SCH-T5** quota breach → `429`, explicit.
- **SCH-T6** jobs survive restart; misfires are delivered late or reported `missed`, never silently lost.
- **SCH-T7** `draft`/`suggest`/`observe` tasks cannot create jobs.

---

*End of 22. Next: `23_ANDROID_CLIENT_PERCEPTION.md`.*
