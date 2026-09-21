# 13_USAGE_RATE_BUDGET.md
## Hypermind Track B — Usage / Rate / Budget

**Package:** subsystem doc 13 of 17 · **Depth:** deep · **Status:** implementation contract
**Authority:** subordinate to `00_CANONICAL_PRD`. Realizes RATE-001..003, USAGE-001..003. Uses `UsageEvent` (`01` §11.2). Enforced against the runtime (`05`), model calls (`06`), tool calls (`07`), scheduler (`22`).
**Consumed by:** `05` bounds, `06`/`07` per-call metering, `02` `429` responses, `14`/`17`.

**Label legend:** `[LOCKED]` · `[IMPL]` · `[FUTURE]` · `[OPEN — OWNER]`.

---

## 0. The rule this document exists to enforce

> **A runaway or malicious agent must not be able to consume unlimited API money, CPU, RAM, network, tool executions, or scheduler jobs — and every consuming call is metered so limits are enforced against real accounting, not guesses.** (RATE-001/002, USAGE-001)

Because the pilot shares one server + paid/local resources across ~10 users, uncontrolled consumption is both a cost risk and a cross-user availability risk (one user's runaway degrades everyone).

---

## 1. The metering substrate (USAGE-001)

`[LOCKED]` Every **model call** and **tool execution** emits exactly one `UsageEvent` (`01` §11.2) *before returning*:
```
UsageEvent { usage_id, request_id, user_id, device_id?, session_id?, graph_id?,
             kind: model_call|tool_call, provider?, model?, tool_id?,
             tokens_or_units, estimated_cost, timestamp }
```
- This ledger is the **single accounting substrate**. Limits (§2) and budgets (§3) are enforced against it — never estimated or guessed (USAGE-002).
- `[LOCKED]` A `UsageEvent` **never contains secret material** (USAGE-003) — provider/model/tool are identifiers, not credentials.
- Metering is **not optional**: a model/tool call that returns without a `UsageEvent` is a defect (a metering gap = an enforcement gap).

---

## 2. Limits (RATE-001)

`[LOCKED]` All of these exist and are enforced deterministically:

| Limit | Scope | Guards against |
|---|---|---|
| per-user rate | user | one user monopolizing/DoS-ing shared server |
| per-device rate | device | a compromised/runaway device |
| per-session concurrency | session | parallel-request floods |
| per-request tool-call cap | request/task | a single task grinding tools (ties `05` bounds) |
| per-request model-call cap | request/task | a single task burning model calls |
| global server limits | server | aggregate overload |
| scheduler limits | user | unbounded job creation (ties `22`) |
| timeout | per call/task | hung calls |
| retry limit | per call | retry storms |
| max task duration | task | never-ending tasks |
| paid-provider budget | user/graph/server | runaway spend (§3) |

`[LOCKED]` `[IMPL]` the numeric values (ties PRD OD-02), but the *existence and enforcement* of every limit is locked. The per-request caps here are the same ceilings `05` enforces in the agent loop — one source of truth, enforced at the runtime.

---

## 3. Budget enforcement (USAGE-002)

`[LOCKED]`
- Per-user / per-graph / per-server **running totals** (calls, tokens/units, estimated cost) are **derived from the `UsageEvent` ledger** (§1), not tracked in a separate guessable counter.
- Before a **paid-provider** call, the runtime checks the projected total against the configured budget; a call that would breach → **refused with an explicit failure** (`429`/explicit "budget exceeded", RATE-001), **not silently made** and not silently dropped.
- Local (ollama) calls have no monetary budget but still count against rate/resource limits.
- `[LOCKED]` A budget breach is surfaced explicitly to the user (FAIL-CORE-001), never a fabricated "done" and never a silent stop.

---

## 4. Counters, quotas, concurrency (`[IMPL, constrained]`)
- **Counters** (rolling windows) implement rate limits; **quotas** implement per-period caps; **concurrency** limits cap simultaneous tasks/calls. `[IMPL]` the mechanism (in-memory + periodic reconcile, or a small store); `[LOCKED]` they are derived-from / consistent-with the ledger, and enforced fail-closed (if the counter can't be read, treat as at-limit rather than unlimited — conservative).

---

## 5. Breach behavior (RATE-003 — must be explicit per limit)

`[LOCKED]` For each limit, the breach behavior is one of, and is chosen explicitly (not left implicit):
- **hard failure** (`429 rate_limited` / explicit error) — e.g. budget breach, per-request caps.
- **graceful degradation** — e.g. reduce concurrency, queue.
- **user notification** — surface an explicit state.
- **administrative alert** — notify the operator (e.g. approaching global budget).

`[REC]` budget + per-request caps → hard failure; concurrency → degrade/queue; global thresholds → admin alert. `[LOCKED]` whatever the choice, breach is never silent and never fails *open* (i.e., never "allow unlimited because the limiter errored").

---

## 6. Cross-user fairness (blast-radius, ties `14`)
`[LOCKED]` Limits are **per-principal**, so one user's runaway is contained to that user's quota and cannot exhaust another user's ability to use the shared server (availability isolation). This is part of the blast-radius story (`14`): a compromised/runaway user degrades themselves, not the tenant base — up to global limits, which protect the server itself.

---

## 7. Observability
`[LOCKED]` Usage totals per user/graph and limit-breach events are visible in the dashboard (`28`) — **secret-free** (DASH-005) and PII-redacted (DASH-006). The operator can see who is consuming what and whether budgets are near, without seeing any credential.

---

## 8. Open items

| ID | Question | Status |
|---|---|---|
| OD-USE-1 | exact numeric limits/budgets | `[IMPL]` (PRD OD-02); existence locked; **owner sets budget ceilings** |
| OD-USE-2 | counter mechanism (in-memory vs store) | `[IMPL]`; ledger-consistent + fail-closed locked |
| OD-USE-3 | budget scope granularity (per-user vs per-graph vs both) at pilot | `[OPEN — OWNER]`; rec per-user + global |

---

## 9. Acceptance hooks (for `17`)

- **US-T1** every model/tool call emits exactly one `UsageEvent` (USAGE-001). *(release-blocking — no metering gap)*
- **US-T2** a `UsageEvent` never contains secret material (USAGE-003).
- **US-T3** a paid call that would breach budget is refused explicitly, not made silently (USAGE-002/RATE-001).
- **US-T4** each limit's breach behavior is explicit and never fails open (RATE-003).
- **US-T5** a runaway agent hits per-request caps and stops explicitly (ties RT-T2).
- **US-T6** one user's rate exhaustion does not deny another user service (per-principal isolation).
- **US-T7** scheduler job creation over quota fails explicitly (ties `22`, FAIL-010).
- **US-T8** budget totals are derived from the ledger, not a separate guessable counter (USAGE-002).

---

*End of 13_USAGE_RATE_BUDGET. Continues to 14 (DEEPEST).*
