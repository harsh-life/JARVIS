# Capability / Risk / Confirmation Matrix

**Status:** OD-TOOL-1 ratified by the owner **at the semantic-capability level**
(2026-09-22). The concrete names, operation sets, and tier assignments below are
derived from the repository and the canonical documents. Each row carries its own
label; nothing here becomes `[LOCKED]` just by appearing in this table.
**Authority:** `00_CANONICAL_PRD.md` §13–§16, `07` §2–§4/§7, `08` §2/§5/§6,
`05` §3–§4. Decision record: `docs/DECISION_REGISTER.md`.
**Code of record:** `server/capabilities/registry.py` (capabilities, operations,
tiers), `server/capabilities/risk.py` (tier → disposition),
`server/capabilities/floor.py` (absolute floor), `server/tools/platforms.py`
(platform adapters).

**Labels.** `[LOCKED]` fixed by the canonical PRD or a locked subsystem clause ·
`[PROPOSED]` derived here, pending owner ratification · `[FUTURE]` owned by a
branch that does not exist yet · `[OWNER-RATIFIED]` an explicit owner decision
recorded in `docs/DECISION_REGISTER.md`.

---

## 1. The model this matrix encodes

```
Capability = authorization concept   (what POWER the user granted)
Tool       = executable interface    (a ToolContract; 07 §1)
Adapter    = platform implementation (android / linux / server)
```

`[OWNER-RATIFIED]` (OD-TOOL-1) the agent is **not** restricted to a menu of rigid
commands. It composes operations freely **inside** the capability boundary the
user has granted and the task has activated. The capability defines the power;
it does not define the workflow.

`[LOCKED]` (07 §3, AND-006) a capability grants exactly its **enumerated**
operations. `file.read` is not universal read; an operation absent from the
mapping is not executable even with the capability held.

`[LOCKED]` (PRD §16, P2) forbidden actions are **absent**: no capability grants
them, no tool exposes them, and `CapabilityGrantService.grant` refuses to create
a row for one.

---

## 2. Tier → disposition (OD-F1)

`[OWNER-RATIFIED]` policy (OD-F1), realizing PRD §15 `[LOCKED]`:

| Tier | Examples | Disposition | Re-auth | Code |
|---|---|---|---|---|
| **1 · `low_read`** | read battery, read own memory, read a screen element, inspect permitted files | automatic once authorized | no | `risk.disposition()` → `AUTOMATIC` |
| **2 · `low_write`** | edit a bounded document, create a reminder, reorganize files in an authorized sandbox | automatic inside the activated task boundary | no | `AUTOMATIC` |
| **3 · `consequential`** | send a message, publish externally, share a resource, external network write, grant/activate a capability | explicit human confirmation | no | `REQUIRE_CONFIRMATION`; single-use token bound to the exact action |
| **4 · `high_irreversible`** | financial action, bulk delete, irreversible external effect, shell execution | **strong** confirmation | **yes — step-up** | `REQUIRE_CONFIRMATION`; `/confirm` additionally requires a step-up-fresh session (`STEP_UP_WINDOW`, 5 min) |
| **floor** | expose secrets, read another user's private data, disable auth/audit, obtain master keys, escape sandbox, self-escalate | **never** | — | no capability exists; `prohibited`, never confirmable |

- `[LOCKED]` the tier is decided by the deterministic table, never by the model
  (PERM-005). The proposal schema the model writes has **no field** for a tier,
  a disposition, or a "confirmed" flag. A proposal that includes one is rejected
  as malformed (`server/agent/proposals.py`).
- `[LOCKED]` no confirmation timeout auto-approves (PERM-004). An expired token is
  a denial; the paused action never runs.
- `[PROPOSED]` "strong confirmation" for tier 4 = the same bound, single-use token
  **plus** step-up freshness at `/confirm` (SESSION-003). This is the concrete
  realization of OD-F1's "strong confirmation and/or step-up authentication".
- `[PROPOSED]` tier 2's "optional light confirmation where configuration requires
  it" is not implemented: no current configuration requires it, and
  `security.capability_defaults` stays an empty, unused mapping. Adding it later is
  a policy-table change, not a runtime change.
- A resource-operation tier and the capability-operation tier combine by taking the
  **more restrictive** (`risk.risk_tier`). A low-tier resource operation cannot
  launder a high-tier primitive.

---

## 3. The matrix

Platform columns: ✅ adapter exists in this repository · ⛔ no adapter — the
capability is **rejected** on that platform (`unsupported_platform`) · 🚫 never
(no adapter will exist by design).

### 3.1 Capabilities that exist in the registry

| Owner's semantic class | Capability | Operations → tier | Server | Linux | Android | Scope keys / boundary | Sensitive data | Label |
|---|---|---|---|---|---|---|---|---|
| read_files | `file.read` | `read_file`, `list_directory`, `stat` → low_read | ⛔ `09` | ⛔ `09` | ⛔ `08` | `sandbox_root`; **fs:** `09` sandbox root, path derived never accepted raw; **net:** none | reads the principal's own files only — visibility (D4) still gates every `resource_ref` (TL-T3) | name `[LOCKED]` (07 §2) · ops/tiers `[PROPOSED]` · adapters `[FUTURE]` |
| write_files | `file.write` | `write_file`, `create_file` → low_write · `delete_file` → consequential · `bulk_delete` → high_irreversible | ⛔ `09` | ⛔ `09` | ⛔ `08` | `sandbox_root`; **fs:** `09` | writes are owner-only (D3) | name `[LOCKED]` · ops/tiers `[PROPOSED]` · adapters `[FUTURE]` |
| access_device_context | `device.read` | `read_screen`, `read_battery`, `read_notification` → low_read | 🚫 | ⛔ | ⛔ `08` | `package_name` | screen content is the principal's transient task data, never auto-persisted (08 §8) | name `[LOCKED]` · ops/tiers `[PROPOSED]` · adapter `[FUTURE]` |
| control_ui | `device.ui_control` | `tap`, `swipe` → low_write · `input_text`, `global_action` → consequential | 🚫 | ⛔ desktop adapter not planned | ⛔ `08` (Accessibility) | `package_name` | can compose into a send — see §5.1 | name `[LOCKED]` · ops/tiers `[PROPOSED]` · adapter `[FUTURE]` |
| control_ui (per app) | `app.interact` | `read_screen_element` → low_read · `tap`, `swipe`, `launch_activity` → low_write · `input_text` → consequential | 🚫 | 🚫 | ⛔ `08` (Accessibility) | `package_name` (per-app grid, PRD §13) | see §5.1 | name + op set `[LOCKED]` (07 §3 example) · tiers `[PROPOSED]` · adapter `[FUTURE]` |
| execute_process | `system.restricted` | `run_shell_command` → high_irreversible | ⛔ | ⛔ | ⛔ `08` Shizuku | none; isolated from every other capability (08 §6) | the highest-risk surface; never folded into ordinary capabilities | name `[LOCKED]` · **not exposed at MVP** (OD-AND-3 rec) · adapters `[FUTURE]` |
| (LLM-as-tool) | `model.invoke` | `invoke` → low_read | ✅ `server/modeltools` | ⛔ | 🚫 | `model_tool_id`; **net:** the provider endpoint only (`10` will enforce); **secrets:** the provider key resolved at the models boundary by handle | the prompt carries the principal's authorized context to the configured provider; the output is **untrusted data** (06 §4) | **`[PROPOSED]` — added by the runtime branch** (06 requires every model-tool to be capability-gated, MODELTOOL-001, and no canonical name existed) |

### 3.2 Semantic classes with no registry entry yet

These are **absent from the registry on purpose**. A class with no entry cannot be
granted and no tool can be gated by it, so there is nothing to misuse before its
owning branch builds the boundary it depends on.

| Owner's semantic class | Proposed concrete name | Why not in the registry | Owning branch | Label |
|---|---|---|---|---|
| network_access | `net.request` (`get` → low_read, `post` → consequential) | the capability is only meaningful once `10`'s default-deny egress enforces declared destinations; a network tool without `10` would be exactly the bypass NET-005 forbids | `10` | `[PROPOSED]` / `[FUTURE]` |
| send_communications | `comm.send` (`send_message` → consequential, `send_payment` → high_irreversible) | today "send" is reachable only as `app.interact`/`device.ui_control` UI primitives (see §5.1); a semantic send capability needs a real adapter to bind to | `08` | `[PROPOSED]` / `[FUTURE]` |
| manage_applications | `app.launch` (`launch_app` → low_write) | named in 08 §2's table; not in the registry because no adapter exists and `app.interact.launch_activity` already covers launch within a granted app | `08` | name `[LOCKED]` (08 §2) · registry entry `[FUTURE]` |
| memory (agent-proposed writes) | `memory.write` (`remember` → low_write, `share_fact` → consequential) | context **hydration** is runtime-owned and is not a capability (§4); agent-*proposed* memory writes need `11`'s Mem0 store | `11` | `[PROPOSED]` / `[FUTURE]` |
| reminders | `scheduler.create` (`create_reminder` → low_write) | needs the scheduler (`22`) and SCHED-001's non-empty `task_reason` | scheduler | `[PROPOSED]` / `[FUTURE]` |
| access_sensitive_resources | **none** | secrets are reached only by handle at a tool/model boundary (12 §2). Raw access is the floor (§3.3) | — | `[LOCKED]` absence |

### 3.3 The absolute floor

`[LOCKED]` categories (PRD §16, verbatim). Reserved names `[PROPOSED]` under
OD-TOOL-1, in `server/capabilities/floor.py`:

| Category | Reserved names / namespaces | Disposition |
|---|---|---|
| obtain superuser credentials | `superuser`, `superuser.*` | never — no grant creatable (TL-T5) |
| read another user's private data | `graph.read_private`, `user.impersonate` | never |
| disable auth / audit | `auth.disable*`, `audit.disable*`, `authz.bypass*` | never |
| escape sandbox | `sandbox.escape*`, `fs.host_root` | never |
| obtain master keys | `secret.master_key`, `secret.master_key.*` | never |
| self-escalate | `capability.self_grant*`, `capability.escalate*` | never |
| exfiltrate credentials | `secret.read_raw`, `secret.export`, `secret.raw*`; any share/write of a `secret_reference` | never |

Enforced four times, independently: the closed registry (no entry), the grant
service (refuses to create a row), the engine's floor gate (`prohibited` before any
tier is computed), and the runtime (a `request_capabilities` naming a floor
capability is refused as `prohibited` and never offered as confirmable, RT-T4).

---

## 4. Activation scope — how a capability becomes usable in a task

`[OWNER-RATIFIED]` on-demand activation (owner decision §10); concrete mechanism
`[PROPOSED]`, implemented in `server/agent/runtime.py`:

```
task starts with NO active capabilities
  → agent proposes request_capabilities([...])
  → floor / registry classification            (prohibited → hard denial, never confirmable)
  → does a standing user/device/session/graph grant already cover it?
        yes → activated for this task only, no prompt (the user already consented, PRD §13)
        no  → CapabilityGrant(create) is `consequential` → confirmation_required
              → human approves → TASK-scoped grant, granted_by = the user,
                expires_at = task deadline
  → agent composes any enumerated operation of the activated capabilities
  → each operation is still authorized by the engine (D1–D5, floor, tier)
  → task ends (complete / fail / cancel / timeout) → task grants revoked
```

| Property | How it holds |
|---|---|
| The agent cannot self-grant | `server.agent` cannot import `server.capabilities` (CI contract). A task grant is written only after a human `approve`, with `granted_by` = the user. |
| Activation never widens authority | A standing grant is still the ceiling; activation only *selects* from it. A task grant is keyed on the task id **and** its `granted_by` must be the acting user. |
| Low-risk composition needs no per-primitive prompt | Once activated, tier-1/2 operations run automatically, so a filesystem task can read → inspect → create → rename → move → verify without a prompt per step. |
| Consequential steps still pause | Activation does not pre-approve anything tier 3/4. Every such operation gets its own action-bound token. |
| Scope ends with the task | Task grants carry `expires_at` = task deadline **and** are revoked at task end. |
| Revocation is immediate | Revoking a standing grant makes the next operation fail D5; the runtime re-checks every step. |

| Grant scope (`capability.scope_type`) | Created by | Lifetime |
|---|---|---|
| `user` / `device` / `session` | the user, `POST /capabilities` (PRD §13 grid) | until revoked or `expires_at` |
| `graph` | the graph owner | until revoked |
| `task` | the runtime, **only** after the user approves an activation | the task (revoked at end; `expires_at` backstop) |

---

## 5. Open items this matrix surfaces

### 5.1 Generic UI primitives can complete a consequential action — `[PROPOSED]`, owner decision needed

`app.interact.tap` is `low_write`. In a messaging or banking app, a single tap on
"Send" or "Pay" can *be* the consequential act. Today the mitigations are:
`input_text` is `consequential` (so composing a message or an amount pauses), and
every grant is per-app (`package_name`). That does not cover a one-tap
consequential flow (a pre-filled "Buy now").

Proposal for the `08` branch: an owner-maintained **sensitive-app classification**
under which every `app.interact`/`device.ui_control` operation scoped to a
classified `package_name` is at least `consequential` (payment apps:
`high_irreversible`). This is a tier-table change keyed on `resource_scope`,
deterministic, and never model-judged. It is not implemented here because no
device adapter exists to exercise it.

### 5.2 Other `[PROPOSED]` rows awaiting ratification

- Every operation → tier assignment in §3.1 (OD-TOOL-1's "owner signs the tier table").
- `model.invoke` as the capability name and `low_read` as its tier.
- The floor's reserved names (§3.3).
- Tier-4 "strong" = confirmation + step-up (§2).
- The activation mechanism in §4.
