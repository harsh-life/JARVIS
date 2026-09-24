# 20_CONFINEMENT_BREAK_GLASS.md
## JARVIS / Hypermind Track B — Confinement Chain & Break-Glass (Unconfined) Execution

**Package:** next-build subsystem contract · **Depth:** deep · **Written:** 2026-09-24
**Status:** formalizes owner decisions **OD-TOOL-1** (the explicit capability → operation → scope → tier → confirmation → confinement chain) and **OD-EXEC-2** (unconfined execution deliberately allowed as break-glass), both owner-ratified 2026-09-24. OD-EXEC-2 was previously `[PROPOSED]` with "never with real data" in `docs/DECISION_REGISTER.md` §2B; the owner's ratified wording (§2 below) supersedes that proposal and must be transcribed into the register (follow-up, `NEXT_BUILD_INDEX.md` §5).
**Authority:** below `00_CANONICAL_PRD.md` and `docs/DECISION_REGISTER.md`. Consumes `07`, `08` §6, `09`, `10`, `docs/CAPABILITY_MATRIX.md`, `docs/OD_A1_BR_T2.md` §3b (row 17).
**Code this plugs into:** `server/execution/confinement.py` (`LANDLOCK`, `UNCONFINED`, `MODES`), `server/execution/process.py` (the `confinement_mode` constructor argument and its startup warning), `server/config/schema.py` (`ProcessExecutionConfig`), `server/security/superuser.py`.

**Labels:** `[LOCKED]` · `[OWNER-RATIFIED]` · `[PROPOSED]` · `[IMPL]` · `[FUTURE]` · `[OPEN — OWNER]`.

---

## 0. The rules this document exists to enforce

> **1. Power comes from composition, not from a tiny command list.** The worker may chain inspect/read/create/edit/move/write freely. Deterministic infrastructure bounds *where* and *how far*, never *how cleverly*.
> **2. Break-glass is recoverability, not convenience.** Unconfined execution exists so the operator can recover a system that confinement itself is blocking. It is never an ordinary mode, never chosen by the model, and never bypasses authorization.

---

## 1. The full chain (OD-TOOL-1)

`[OWNER-RATIFIED]` the relationship is:

```
capability → operation → resource scope → risk tier → confirmation → confinement
```

`docs/CAPABILITY_MATRIX.md` §3 already holds the first four columns as code of record. This table adds the **confinement** column, which no existing document states in one place. Tiers are the matrix's `[PROPOSED]` values; this document does not change them.

| Capability | Operations → tier | Resource scope | Confirmation | Confinement enforced by |
|---|---|---|---|---|
| `file.read` | `read_file`, `list_directory`, `stat` → low_read | `sandbox_root` label (never a path); root derived from the server-side user/graph id | automatic | `09` **mediated** sandbox: `dir_fd` walk, `O_NOFOLLOW` every hop |
| `file.write` | `write_file`, `create_file` → low_write · `delete_file` → consequential · `bulk_delete` → high_irreversible | same | automatic / confirm / confirm + step-up | `09` mediated sandbox + per-principal quota (OD-FS-2) |
| `net.request` | `get` → low_read · `post` → consequential | operator `EgressPolicy` destinations | automatic / confirm | `10` **mediated_proxy**: default deny, checked-IP = connected-IP, metadata/loopback always blocked |
| `model.invoke` | `invoke` → low_read | `model_tool_id` | automatic | provider endpoint only; key by handle at the models boundary |
| `system.restricted` | `run_shell_command` → high_irreversible | executable allow-list (operator) | confirm + step-up **every run** | **Landlock + seccomp** per child (OD-EXEC-1); argv only; env from scratch; rlimits; process-group kill. **Break-glass (§2) removes only the kernel ruleset.** |
| `device.read` | `read_screen`, `read_battery`, `read_notification` → low_read | `package_name` | automatic | device-side guard (`08` §4, `23` §5); perception ladder (`23` §6) |
| `app.interact` | `read_screen_element` → low_read · `tap`, `swipe`, `launch_activity` → low_write · `input_text` → consequential | `package_name` (required, OD-DEV-1) | automatic / confirm; **sensitive apps raise tiers** (matrix §5.1, open) | device-side guard; exact `device_id` delivery |
| `device.ui_control` | `tap`, `swipe` → low_write · `input_text`, `global_action` → consequential | `package_name` | automatic / confirm | same |

Composition rule `[LOCKED]` (matrix §4): once a capability is activated for a task, tier-1/2 operations chain automatically — read → inspect → create → rename → move → verify runs with no prompt per step. Each operation is still individually authorized by `04`. Consequential operations get their own action-bound token.

Protected resources `[LOCKED]`: the floor (matrix §3.3) and `09` §5's sensitive paths are unreachable by construction; no capability, confirmation, or break-glass reaches them *through a tool*. (§4 states what break-glass does expose at the OS level.)

---

## 2. Break-glass — the owner's decision

`[OWNER-RATIFIED]` (OD-EXEC-2): unconfined execution is deliberately allowed, for emergency recovery, operator intervention, exceptional debugging/development, and situations where confinement itself prevents recovery. It must be: off by default · explicit · authenticated · audited · task-bound · capability-bound · never silently enabled · never casually chosen by the model · not an ordinary low-risk capability · no bypass of authorization · retaining resource/time/process/output limits · cancellable · observable.

### 2.1 Exact meaning of "unconfined"

`[PROPOSED]` break-glass removes **exactly one layer**: the Landlock filesystem/TCP ruleset and the seccomp filter applied to a `system.restricted` child process.

| Still enforced under break-glass | Removed under break-glass |
|---|---|
| `04` authorization of the operation, as the principal | Landlock filesystem rules |
| `system.restricted` activation + `high_irreversible` confirmation + step-up | Landlock TCP rules |
| executable allow-list (a separate, explicit break-glass list — §2.3) | seccomp `socket()` / `io_uring` denial |
| argv-only exec, never a shell string | |
| from-scratch environment; `LD_*`/`DYLD_*`/`GCONV_PATH` refused (OD-EXEC-3) | |
| rlimits, timeout, output caps, process-group kill, `/cancel` | |
| metering, audit, rate limits, bounds | |
| `09` sandbox for `file.*` tools and `10` policy for `net.request` (separate tools, untouched) | |

Break-glass never disables authentication, authorization, audit, the SecretStore boundary, or any other tool's boundary.

### 2.2 Two keys

`[PROPOSED]`

1. **Operator enablement (static).** `execution.process.break_glass.enabled: false` by default. When `true`, the server logs a startup warning and shows a persistent banner in `28`. Enablement only makes activation *possible*.
2. **Per-task activation (dynamic).** A **superuser** issues a break-glass authorization via `POST /api/v1/admin/control/break-glass` bound to: `task_id`, owning `user_id`, the exact executables allowed, `max_invocations` (default 1), `expires_at` (default ≤ 15 minutes, never past the task deadline), and a mandatory human-readable `reason`.

`[PROPOSED, security-critical]` **only the superuser can activate.** BR-T2 §3b row 17 measured that an unconfined command can read another user's files and the server's environment. Letting an ordinary user activate it would hand a user a path into other users' private data — a floor category (PRD §16). On a single-owner deployment the owner *is* the superuser, so this costs nothing in practice.

### 2.3 Model restrictions

- `[PROPOSED]` there is **no proposal field** for confinement mode. The worker cannot request, name, or select unconfined execution. A proposal carrying such a field is rejected as malformed (same mechanism as the existing ban on tier/"confirmed" fields in `server/agent/proposals.py`).
- `[PROPOSED]` the executor chooses the mode from the break-glass record, not from the operation arguments.
- `[PROPOSED]` break-glass executables are a **separate list** (`break_glass.allowed_executables`) from the normal allow-list, so enabling break-glass never silently widens ordinary shell access.

### 2.4 Lifecycle

```
superuser activates (task, user, executables, max_invocations, expiry, reason)   → audit break_glass.activated
  worker proposes run_shell_command → 04 authorize → confirm + step-up by the task owner
  executor sees a live break-glass record for this task + executable → runs unconfined
      → audit break_glass.invoked {task, executable, argv hash, exit, duration}
  invocations exhausted | expiry | task ends | superuser revokes | breaker trips      → audit break_glass.ended
```

- Task-bound: the record dies with the task (complete / fail / cancel / trip).
- Cancellable: `/cancel` and the breaker kill the unconfined process group exactly as for a confined one.
- Observable: the task status carries `break_glass_active: true` to the user's device UI and to `28`.

### 2.5 Migration from today's code

Today `confinement_mode: unconfined` is a **global** operator setting that applies to every `system.restricted` run (it logs a warning when executables are allow-listed). That does not meet OD-EXEC-2's task-bound and authenticated requirements.

`[PROPOSED]` in the next build:
- `execution.process.confinement_mode` accepts only `landlock`. A config still set to `unconfined` fails validation at startup with a message pointing to `break_glass` (fail-closed config, `15` §6).
- Hosts without Landlock (including macOS) continue to fail `PLATFORM_UNSUPPORTED` for `system.restricted`, as OD-EXEC-1 already does. Development on such hosts uses Linux or WSL2, or a per-task break-glass activation with disposable data.

`[OPEN — OWNER]` OD-BG-2: whether a disposable-data development host may keep a global unconfined switch under a clearly separate name (e.g. `dev_insecure_process_mode`) that refuses to start if any non-test user exists.

---

## 3. Recovery role

Break-glass is the operator's tool for the situations `18` cannot handle: a confinement rule that blocks a legitimate repair, a wedged sandbox, a diagnostic that needs a raw socket. It is not how `18`'s worker recovery works — worker switches never touch confinement.

---

## 4. Threat model

| Threat | Result under this design |
|---|---|
| Worker/model tries to run unconfined | impossible: no field; executor reads the record, not the proposal |
| Prompt injection asks for break-glass | same; activation is a superuser HTTP action outside the agent path |
| Compromised ordinary user session | cannot activate; still needs step-up per run even when a record exists |
| Stolen superuser token | full operator compromise — already OD-A1 territory (`server/security/superuser.py` states this bound); break-glass adds audit, not a new exposure class |
| Unconfined child reads other users' data / server env | **real and accepted only inside an active window.** This is BR-T2 row 17's class. With real, non-disposable data on a multi-user server, every activation is a cross-user exposure event and must be treated as one (the mandatory `reason` is recorded) |
| Operator misuse | audited with reason, executables, argv hash; visible in `28` |
| Forgotten activation | expires by time, invocation count, and task end — whichever first |

`[LOCKED]` OD-A1 stays an accepted residual, not an isolation claim. Break-glass does not change that; it adds one more documented, audited way to reach the residual.

---

## 5. Configuration (`[PROPOSED]`)

```yaml
execution:
  process:
    confinement_mode: landlock          # only valid value in the next build
    allowed_executables: []             # exists
    break_glass:
      enabled: false
      allowed_executables: []
      max_window_minutes: 15
      max_invocations: 1
```

---

## 6. Open items

| ID | Question | Status |
|---|---|---|
| OD-BG-1 | Transcribe the ratified OD-EXEC-2 into `DECISION_REGISTER.md` §2B (replacing the proposal) | follow-up edit, owner to authorize |
| OD-BG-2 | Disposable-host development switch (§2.5) | `[OPEN — OWNER]` |
| OD-BG-3 | Whether break-glass should ever extend beyond `system.restricted` (e.g. temporary egress widening) | `[OPEN — OWNER]`; recommended **no** for the next build |

---

## 7. Acceptance hooks (`[PROPOSED]` IDs)

- **BG-T1** with `break_glass.enabled: false`, no path runs a child without Landlock + seccomp.
- **BG-T2** a config with `confinement_mode: unconfined` fails to start.
- **BG-T3** an ordinary user token cannot activate break-glass; a superuser can.
- **BG-T4** a proposal carrying any confinement/unconfined field is rejected as malformed.
- **BG-T5** under an active record, the run still requires `system.restricted` activation, confirmation, and step-up.
- **BG-T6** an executable not on `break_glass.allowed_executables` is refused even with a live record.
- **BG-T7** the record ends at expiry, at `max_invocations`, at task end, on revoke, and on a breaker trip — whichever first.
- **BG-T8** timeout, output caps, rlimits, and `/cancel` process-group kill apply to unconfined runs.
- **BG-T9** every activation, invocation and end emits its audit event with the reason; `28` shows the active banner.
- **BG-T10** BR-T2 is re-run with break-glass active and row 17's reachable set is recorded, not claimed closed.

---

*End of 20. Next: `21_MEMORY_PROVIDER_VAULT.md`.*
