# NEXT_BUILD_INDEX.md
## JARVIS / Hypermind Track B — Next-Build Documentation Index

**Written:** 2026-09-24 · **Audited against:** `harsh-life/JARVIS` `main` @ `4223c5f` (identical tree to `integration-hardening` @ `765f4ac`).
**Purpose:** the map for the documents added for the next build. `TRACK_B_ARCHITECTURE_INDEX.md` records "17/17 complete" and is **not edited**; this file sits beside it.
**Place in the authority order** (`docs/DECISION_REGISTER.md`): `00_CANONICAL_PRD.md` > decision register > subsystem contracts (`01`–`17`, `26`, and these) > implementation > proposals. None of these documents edits an existing one. Where the owner's 2026-09-24 decisions conflict with `[LOCKED]` PRD text, the conflict is listed in §4, not silently resolved.

---

## 1. New documents

| Doc | Why it is needed | Plugs into | Formalizes |
|---|---|---|---|
| `18_SUPERVISORY_RUNTIME_RECOVERY_CONTROL.md` | `05` has one-hop provider fallback only; no stall/malformed recovery, no worker chain, no circuit breaker with independent triggers, no task modes | `server/agent/runtime.py`, `state.py`, `bounds.py`, `composition/models.py` | RECOVERY-1; breaker half of the Judge decision; OD-F1 suggest/draft; Darwin worker boundary |
| `19_JUDGE_EVALUATION.md` | the Judge is new; nothing in the package defines it | audit/usage ledgers, breaker port, `server/models/` | Judge decision; self-improvement with human approval |
| `20_CONFINEMENT_BREAK_GLASS.md` | OD-EXEC-2 is ratified but the code has only a **global** `unconfined` switch; the capability→…→confinement chain is not stated in one place | `server/execution/confinement.py`, `process.py`, config | OD-EXEC-2; OD-TOOL-1 chain |
| `21_MEMORY_PROVIDER_VAULT.md` | `11` defines semantics but no provider contract; Mem0's own LLM calls would bypass metering and egress as PRD §41 is written | `server/memory/hydration.py`, `server/vault/`, `02` §7–§8 | MemoryProvider + Mem0 OSS (not forked) + separate Vault |
| `22_SCHEDULER.md` | fire-time behaviour is undefined; `13`/`14` already cite `22` | `server/scheduler/`, `01` §6.2, `02` §9 | scheduler decision |
| `23_ANDROID_CLIENT_PERCEPTION.md` | `08` is server-side only; no client, channel, or perception ladder is specified; `android/` does not exist | `server/execution/android.py`, `server/auth/device.py` | Android decisions; donor reuse rules; character deferral hook |
| `27_VOICE.md` | `02`/`14`/`15` already cite `27`; provider and confirmation rules missing | `server/voice/`, `01` §12.1, `02` §10 | voice decision |
| `28_DASHBOARD_OPERATOR_CONSOLE.md` | `02`/`13`/`14`/`16` already cite `28`; the owner's "control surface" meets locked DASH-002 | `server/dashboard/`, `/admin/*` | dashboard decision |

**Why scheduler, voice and dashboard are three files, not one:** existing documents (which may not be edited) already cite `22`, `27` and `28` as separate documents. Merging them would leave those references dangling. Each is kept compact.

---

## 2. Requested topics deliberately not given their own document

| Topic | Where it lives instead | Reason |
|---|---|---|
| Capability / operation / risk / confirmation model | `docs/CAPABILITY_MATRIX.md` + `07` (existing) and `20` §1 (adds the confinement column) | already fully covered in code-of-record form; a new doc would duplicate it |
| Deterministic emergency control | `18` §5–§6 | the breaker is runtime lifecycle; splitting it from recovery would separate "stop" from "what happens after stop" |
| Darwin / Blackbox boundary | `18` §8 (the worker slot) | the whole required boundary is one interface; `26` and PRD §25 already cover the adjacent sockets |
| Self-improvement / human oversight | `19` §9 | improvement signals originate in the Judge; the boundary is fully contained there |
| Planner | §3 below | the dependencies are real but short; they belong in this index |

---

## 3. Build sequence

Order is by dependency and by the real-data gate (`17` §5), not by document number.

```
Stage 1 — runtime foundations (no new external dependencies)
  18 worker slot refactor, task modes, worker chain, stall/loop detection, breaker + operator stop
  20 break-glass records; remove global unconfined; confinement chain tests
      ↳ both change only server code that already exists and is tested

Stage 2 — real-data critical path, part 1
  21 MemoryProvider + Mem0 adapter + write gate + Vault reindex
      ↳ MEM-T1 on a real store; BR-T2 re-run for memory

Stage 3 — device channel (unblocks 22 delivery, 23 execution, 27 on-device voice)
  23 login handoff (App Link + stable hostname) → WebSocket channel + push wake → fake transport
     → client shell, overlay, perception ladder, device.read
     → UI control ONLY after the sensitive-app classification is ratified
      ↳ AND-T*; BR-T2 re-run for Android

Stage 4 — services on top of the channel
  22 scheduler (needs the 23 channel to deliver)
  27 voice (on-device default needs only the client)

Stage 5 — oversight and observation (consume everything above)
  19 Judge post-hoc, then live monitoring behind may_request_stop
  28 read-only console + control endpoints from 18/19/20

Real-data gate opens only when: 17 §5's release-blocking set is green, including MEM-T1 on real Mem0
(stage 2) and 08's suite on a real device (stage 3), and BR-T2 has been re-run for both.
OD-A1 remains an accepted residual throughout — never an isolation claim.
```

Parallel-safe: stage 1 and the client-side UI shell in stage 3 touch disjoint code. The presentation character is out of scope until stage 5 is done (`23` §7).

---

## 4. Conflicts with locked text — owner action required

These are real conflicts between the owner's 2026-09-24 decisions and `[LOCKED]` canonical text. The new documents work within the locked text and flag each one; none is silently overridden.

| # | Owner decision | Locked text it meets | What the new docs do | Owner action |
|---|---|---|---|---|
| C1 | Remote/online LLM is the current default | PRD §19 and `06` §2 `[LOCKED]`: default and recommended primary is a local Ollama model; register OD-MT-2 "default local" | Nothing in code blocks a remote primary — it is a config choice. The pilot's operator config selects it. | Amend PRD §19 / `06` §2 and close OD-MT-2 in the register. **Also:** `per_task_budget` and `security.budgets` default to **0.0** (OD-02, OD-USE-1), which refuses every paid call — a remote default will fail every task with `budget_exceeded` until budgets are raised. |
| C2 | Dashboard as a control surface | PRD §28 DASH-002 `[LOCKED]`: read-only, no mutation path | `28` keeps the dashboard read-only and puts controls in separate superuser endpoints | Ratify OD-DASH-1 (recommended) or amend DASH-002 |
| C3 | Mem0 OSS with metering and no hidden egress | PRD §41 `[LOCKED]` config routes Mem0's LLM straight to `litellm/deepseek` with an env key (unmetered, undeclared egress) | `21` §2.2 reads §41 as "never default to OpenAI; provider is config" and routes Mem0's model use through JARVIS | Confirm OD-MEM-A |
| C4 | OD-EXEC-2 break-glass allowed | Register §2B's `[PROPOSED]` OD-EXEC-2 text ("never with real data", global mode) | `20` specifies the ratified task-bound, superuser-activated form | Transcribe OD-EXEC-2 into the register (OD-BG-1) |

---

## 5. Follow-up edits to existing documents (not made; owner to authorize)

| Existing doc | Edit |
|---|---|
| `docs/DECISION_REGISTER.md` | record 2026-09-24 decisions: RECOVERY-1, Judge, OD-EXEC-2 (ratified form), memory/vault, Android, scheduler, dashboard, voice, remote default (C1), Darwin deferred |
| `docs/CAPABILITY_MATRIX.md` | add `capture_screenshot` (`23` §6), confirm `scheduler.create` (`22`), optional `vault.query` (`21` §5), the mode ceiling note (`18` §3), the confinement column (`20` §1) |
| `01` §1.2 enum registry | `AgentFailureCode`: `stalled`, `worker_chain_exhausted`, `emergency_stop`; task `mode`; optional `usage.kind: evaluation_call` (OD-JDG-4) |
| `02` | `mode` on task submission; `/admin/control/*`; `device_unavailable` client state; device channel endpoint |
| `05` §5 / §11 | point to `18` for recovery and the worker slot |
| `17` | add the SUP/JDG/BG/MP/SCH/ANDC/VOI/DSH hooks; keep them out of the release-blocking set except MP-T1 and the Android suite, which are already gating |
| `TRACK_B_ARCHITECTURE_INDEX.md` | list the new documents |
| PRD §19, §28, §41 | per C1–C3 |

---

## 6. Genuinely unresolved questions that block work

Only these block implementation; every other open item has a recommended default.

1. **Sensitive-app classification** (matrix §5.1). Blocks Android UI control. Perception and `device.read` can ship without it.
2. **C1 budgets.** A remote default cannot run a single task until `per_task_budget` and `security.budgets` are set above zero.
3. **Stable HTTPS hostname** (named tunnel) for the Google redirect URI and Android App Link. Blocks Android login.

---

## 7. Darwin / Blackbox

Deferred; not part of the current runtime. The only boundary documented is the **worker slot** in `18` §8: a future reasoning/chaining engine is another `Worker` behind the same parser, bounds, metering, authorization, tiers, confirmation and confinement, injected by the composition root and never imported by `server/agent`. No other Darwin document is created.

---

*End of index.*
