# Character presentation — how it works and what can be configured

Owner decisions **OD-CHAR-1** (the default character) and **OD-CHAR-4** (the
character expresses JARVIS's operational activity): `docs/DECISION_REGISTER.md`
§2M, PRD §23 amendments. OD-CHAR-2's selection from the current utterance is
superseded by OD-CHAR-4 and retired (§10). This page is the reference for
client implementers and for the future configuration website.

The character expresses **what JARVIS is doing** — how much work, how it is
going, how recently it was asked — not an inferred human emotion, and not a
claim that JARVIS has feelings. Its names (`happy`, `angry` …) are the names of
seven drawings, chosen to be readable at a glance:

| Expression | Presents |
|---|---|
| `happy` | frequent, meaningful requests and productive work |
| `satisfied` | normal, steady operation |
| `default` | the neutral baseline |
| `sleepy` | sustained inactivity |
| `surprised` | an unexpected operational event (an internal error, a lost dependency, the circuit breaker) |
| `angry` | excessive workload or sustained overload — **never anger at the user** |
| `sad` | failed, interrupted or unsuccessful work |

## 1. The parts

```
 agent_tasks (the user's own rows) ─▶ activity scorer ─▶ GET /presentation/activity ─┐
                                      (server, pure)       (owner-scoped, no-store)    │
                                                                                      ▼
 local activity (app opened, voice, request submitted) ─▶ activity policy ─▶ controller ─▶ renderer
                                                          (client, pure)     (+preview)    (rig / images)

 operational status ───────────────────────────────────────────────────────▶ status indicator (unchanged)
```

| Part | What it is | Where | Contract |
|---|---|---|---|
| Activity scorer ("Central AI") | turns the caller's task rows into a 1–10 score per state, deterministically | `server/presentation/activity.py` | `ActivityPresentation`; §6 |
| Activity report | the scores and the components behind them, per request | `GET /api/v1/presentation/activity` | `shared/android/presentation_activity_samples.json` |
| Activity policy | turns a stream of reports into one shown state: hysteresis, cooldowns, staleness, local wake, intensity | `ActivityPolicy` (Python reference; Android `ActivityPolicy.kt`) | `shared/android/presentation_activity_policy_vectors.json`; §3 |
| Controller | runs the policy; adds the development preview (pin) | Android `CharacterController`, run by `CharacterPresentation` | — |
| Feed | asks for the report while the screen is in front, and sooner when the current task changes | Android `CharacterActivityFeed` | — |
| Asset profile | which image shows each state | `CharacterProfile`, `assets/character/<id>/profile.json` | `CharacterProfile` |
| Renderer | draws the shown state at its intensity: the rig (`docs/CHARACTER_RIG.md`) or the images | `presentation/ui/Character.kt`, `presentation/character/rig/` | unchanged |
| Operational status | what JARVIS is actually doing | `Presenter` → `StatusIndicator` (untouched) | — |

"Central AI" here is the server runtime, not a model: **no LLM or classifier
judges a presentation.** The scorer is ordinary deterministic code, and the
model layer is outside what it may import (§8).

## 2. The activity report

`GET /api/v1/presentation/activity` (bearer token; the caller's own data only;
`Cache-Control: no-store`) returns, validated whole by `ActivityPresentation`:

```json
{
  "schema_version": 1,
  "computed_at": "2026-11-10T12:00:00Z",
  "window_s": 1800,
  "scores":     {"happy": 8, "satisfied": 8, "default": 5, "sleepy": 1, "surprised": 1, "angry": 2, "sad": 1},
  "components": {"engagement": 61, "productivity": 88, "steadiness": 100, "failure": 0,
                 "pressure": 8, "surprise": 0, "inactivity": 0}
}
```

- `scores` — every state, integers 1–10: how well that state presents the
  current activity.
- `components` — integers 0–100, the measured inputs (§6), sent so that any
  score can be explained. They describe JARVIS's work, not the user.
- `computed_at` is informational. A client judges staleness by when *it*
  received the report, on its own monotonic clock, so a skewed phone clock
  cannot make a report look fresh or stale.
- One unknown, missing, mistyped or out-of-range field rejects the report;
  a rejected report is discarded and changes nothing.

## 3. The activity policy (client)

Pure, in memory, held to the shared vectors. At each report, local activity or
tick:

1. **Evidence.** The last valid report is evidence until `activity.max_age_ms`
   (60 s) after it was received.
2. **No evidence** (signed out, offline, stale): the baseline only — `default`,
   or `sleepy` after `timing.idle_timeout_ms` (5 min) without local activity —
   at the baselines' fixed intensities. This is also the behaviour before
   sign-in.
3. **Local wake.** Local activity (opening the app, starting voice input,
   submitting a request) within `idle_timeout_ms` makes `sleepy` ineligible,
   whatever the report says. Someone who is here is answered.
4. **Enter** a non-default state only when its score reaches its threshold and
   it is not cooling down.
5. **Hysteresis.** The shown state stays while its score is at least
   `threshold − activity.exit_margin` (2). Entry needs the threshold, so a score
   hovering near it cannot flicker the face.
6. **Choice.** The highest-scoring eligible state. The shown state wins a tie;
   otherwise ties go to the owner's order: **happy, satisfied, default, sleepy,
   surprised, angry, sad**. Nothing eligible → `default`.
7. **Minimum display.** While the shown state still holds, a different choice
   replaces it only after `timing.min_display_ms` (1.5 s), unless it scores at
   least `timing.interrupt_score` (9). A state that no longer holds leaves at once.
8. **Transient `surprised`.** It is an event, not a condition: it leaves after
   `timing.hold_ms.surprised` (2.5 s) whatever its score. Every other state
   stays as long as its evidence does.
9. **Cooldown.** After a state leaves the screen it cannot be entered again for
   `activity.cooldown_ms[state]`: happy 30 s, satisfied 15 s, sleepy 30 s,
   **surprised 60 s**, **angry 60 s**, sad 30 s. A baseline `sleepy` (rule 2)
   has none.

The client asks for a report every `activity.poll_interval_ms` (15 s) while the
screen is started, and once more soon after the current task changes (never
within 2 s of the last request). In the background it does not ask at all.

### Worked transitions (all in the shared vectors)

- *Unexpected failure:* `satisfied` → `surprised` at once (score 10 ≥ interrupt)
  → after 2.5 s, `sad` (the failure) → `surprised` cannot recur for 60 s.
- *Overload and recovery:* `angry` at 8 (intensity 4) → 10 (10) → 9 (7) → 8 (4) →
  held at 7 and 6 → leaves at 5 → `satisfied` if steady work remains.
- *Increasing interaction:* `satisfied` 8 → `happy` 8 ties and the shown state
  stays → `happy` 9 outscores it and takes over.

## 3a. Presentation intensity

Two numbers, kept apart: the **score** (how well a state presents the
activity; the policy chooses with it) and the **intensity** (1–10, how strongly
the chosen state is animated). No model judges intensity, and nothing that
authenticates, authorizes, confirms, budgets or executes reads it.

**Mapping** (`presentation_intensity`; Android `IntensityPolicy`), one rule for
every state, using that state's own threshold:

    intensity = floor + round_half_up((s − threshold) × (10 − floor) / (10 − threshold)),
    s = max(score, threshold)

integers only, `floor` 4. Lifting a held score to its threshold means a state
kept by hysteresis below its threshold presents at the floor.

| Score | Threshold 7 (happy, satisfied, surprised) | Threshold 8 (sleepy, angry, sad) |
|---:|---:|---:|
| 6 (held) | 4 | 4 |
| 7 | 4 | 4 (held) |
| 8 | 6 | 4 |
| 9 | 8 | 7 |
| 10 | 10 | 10 |

- **Baselines without evidence** take fixed intensities: `default` 5, `sleepy` 2.
  `default` is always 5. A `sleepy` chosen from a report takes its mapped
  intensity, so deep inactivity presents a sleepier face than a short lull.
- **The shown state** keeps its intensity unless the new one differs by at least
  `intensity.dead_band` (2); nothing restarts either way.
- **The debug preview** pins at 5 unless another intensity is chosen.

The rig turns an intensity into posture emphasis and idle-motion gains
(`docs/CHARACTER_RIG.md` §5); 5 and 6 are the canonical animation exactly.

## 4. Settings the configuration website will configure

Two independent documents, both validated by `shared/schemas/presentation.py`
on the server and by the same rules on every client (an invalid document is
rejected whole and the client keeps its built-in defaults).

**Presentation settings** (`CharacterPresentationSettings`; defaults in
`assets/character/presentation.json`):

| Field | Default | Range | Used by the activity policy |
|---|---|---|---|
| `visible` | `true` | bool | renderer |
| `profile_id` | `"default"` | `[a-z0-9][a-z0-9_-]{0,63}` | renderer |
| `transition` | `"crossfade"` | `crossfade` \| `none` | renderer |
| `reduced_motion` | `"system"` | `system` \| `always` | renderer |
| `thresholds.<state>` | happy 7, satisfied 7, default 1, sleepy 8, surprised 7, angry 8, sad 8 | 1–10 | entry threshold (rule 4) and intensity |
| `timing.transition_ms` | 220 | 0–2000 | renderer |
| `timing.min_display_ms` | 1500 | 0–30000 | rule 7 |
| `timing.hold_ms.surprised` | 2500 | 500–60000 | rule 8 |
| `timing.idle_timeout_ms` | 300000 | 10000–86400000 | rules 2 and 3 |
| `timing.interrupt_score` | 9 | 1–10 | rule 7 |
| `intensity.floor` | 4 | 1–10 | §3a |
| `intensity.dead_band` | 2 | 0–9 | §3a |
| `intensity.baseline.default`, `.sleepy` | 5, 2 | 1–10 | rule 2 |
| `activity.poll_interval_ms` | 15000 | 5000–300000 | the feed |
| `activity.max_age_ms` | 60000 | 10000–600000, > poll interval | rule 1 |
| `activity.exit_margin` | 2 | 0–5 | rule 5 |
| `activity.cooldown_ms.<state>` | happy 30000, satisfied 15000, sleepy 30000, surprised 60000, angry 60000, sad 30000 | 0–600000 (no `default`) | rule 9 |
| `min_confidence` | 0.6 | 0–1 | **not used** (kept so stored documents stay valid) |
| `timing.max_result_age_ms` | 5000 | 100–60000 | **not used** (see `activity.max_age_ms`) |
| `timing.hold_ms.<other states>` | 4000–5000 | 500–60000 | **not used** (only `surprised` is transient) |

`intensity` and `activity` are optional (schema version 1): a document without
them takes these defaults, so every stored document stays valid. Thresholds,
cooldowns and the scorer's constants are product parameters to be tuned on real
use, not scientifically validated cut-offs.

**Asset profile** (`CharacterProfile`): `profile_id`, `fallback`, and a `states`
map from state to a bare `.png`/`.webp` file name in that profile's directory.

**Server store (pilot plan C10).** `GET/PUT/DELETE /api/v1/config/presentation`
keeps one validated `CharacterPresentationSettings` per user (table
`presentation_settings`) — exactly this document, refused whole if anything is
unknown or out of range — and nothing on the server reads it to decide anything.
The Android client ships `presentation.json` as its defaults and does not fetch
the stored settings yet.

## 5. Signals: what exists, what does not

Inventory of the running system on 2026-11-10. Everything the scorer uses is a
column the runtime already writes to `agent_tasks` for its own reasons.

| Requested signal | What the scorer uses | Status |
|---|---|---|
| Interaction frequency | present-user tasks (`session_id` set) created in the window, decayed | **real** |
| Work performed | `model_calls + tool_calls` of tasks updated in the window, decayed | **real** (counts of calls, not their size or duration) |
| Nature of the work | completions that used a tool weigh 1.0, answer-only completions 0.6 | **proxy** — a tool call stands in for "did something" |
| Impact and completion | completed vs failed tasks, decayed | **partial** — success is measurable, *impact* is not: nothing records whether a result mattered |
| Failed / interrupted work | `status = failed`, every failure code | **real** |
| User cancellations | `status = cancelled` | **real, deliberately ignored** (a user's decision is not a failure) |
| Time / recency | ages of the timestamps above; decay half-lives | **real** |
| Workload pressure | live tasks (running, awaiting confirmation, waiting for platform), recent calls, ceiling/rate-limit/budget stops | **real** — there is no queue: a waiting task is paused, never queued |
| Sustained demand | the decayed sums above over a 30-minute window | **real** |
| Inactivity | time since the latest `updated_at` within 24 h; none → fully idle | **real** |
| Unexpected events | unexpected failure codes; a task entering `waiting_for_platform`; a worker switch (counted when its task ends) | **partial** — the runtime records no time for a worker switch, so it is seen late |
| Latency, CPU, model token use, provider errors that did not fail a task | — | **not available to the scorer**; not invented |
| Scheduler reminders, agent inbox items | — | exist, but are not work JARVIS performed for a request; not used |

## 6. Scoring: formulas, constants, thresholds

All in `server/presentation/activity.py`. For each task row touching the window
(created, updated or finished in the last 30 minutes, or still live; at most 500
rows) with age *a* of the relevant timestamp, `d(a, h) = 0.5^(a/h)` (a future
timestamp counts as now). Sums are taken in a fixed row order, so the result
does not depend on how rows arrive.

**Raw sums**

| Sum | Over | Timestamp | Half-life |
|---|---|---|---|
| engagement | present-user tasks | created | 10 min |
| productivity | completed tasks × (1.0 if any tool call else 0.6) | finished | 10 min |
| steadiness | completed tasks | finished | 10 min |
| failure | failed tasks (cancelled excluded) | finished | 5 min |
| work | every task × (model_calls + tool_calls) | updated | 10 min |
| strain | failed with `max_*_exceeded`, `wall_clock_timeout`, `rate_limited`, `budget_exceeded`, `agent_budget_exhausted` | finished | 10 min |
| surprise | failed with `internal_error`, `stalled`, `worker_chain_exhausted`, `emergency_stop`, `confirmation_state_lost`, `platform_unavailable`, `principal_revoked`, `model_unavailable`, `spec_changed`; ended tasks with a worker switch; live tasks in `waiting_for_platform` | finished / updated | 45 s |

**Components** (each clamped to 0–1, reported ×100)

    E  = engagement / 4          P = productivity / 3        St = steadiness / 1
    F  = failure / 1.25          S = surprise / 1
    L  = max(live / 3, work / 160, strain / 2)                          (pressure)
    Z  = 0 if any task is live; 1 if no activity in 24 h;
         else clamp((idle − 2 min) / 8 min)                              (inactivity)
    O  = clamp((L − 0.5) / 0.5)                                          (overload)
    W  = 0.7 if a task is live and L < 0.5, else 0                       (working)

**Levels → scores** (`score = 1 + round_half_up(9 × level)`)

| State | Level | Notes |
|---|---|---|
| happy | √(E·P) · (1 − F) · (1 − O) | needs both the user's requests *and* completed work; failure and overload damp it |
| satisfied | min(0.78, max(St, W)) · (1 − F) · (1 − O) | capped at score 8, so fully evidenced `happy` outranks it |
| default | 0.45 | always 5: below every other threshold |
| sleepy | Z | ≥ 8 after about 7.8 min idle; 10 at 10 min |
| surprised | S | one fresh unexpected event is 10; below 7 within about 30 s |
| angry | L | workload only — the request count is not an input |
| sad | F · (1 − 0.6·St) | a completion after a failure is recovery |

**What it takes, at the default thresholds** (from `tests/presentation/test_activity_scoring.py`)

| Situation | Result |
|---|---|
| one completed task just now | satisfied 8 |
| three productive tasks in six minutes | happy 8, satisfied 8 → happy (order) |
| five productive tasks in ten minutes | happy 10 |
| one task running, light load | satisfied 7 |
| four ceiling-heavy tasks (16 model + 24 tool calls each) in eight minutes | angry 8, happy damped to 4 |
| three concurrent live tasks | angry 10 |
| a task stopped by `max_iterations_exceeded` just now | sad 8 |
| an `internal_error` just now | surprised 10, then sad 8 |
| a failure two minutes ago, then a success | sad 3: recovered |
| three cancellations | nothing (sad 1) |
| nothing for ten minutes | sleepy 10 (only once the user has been away locally for 5 min too) |

## 7. What it never does

- **Never an authority.** No score, component or expression influences
  authentication, identity, membership, ownership, capabilities, policy, risk
  classification, confirmation or step-up, budgets, or tool authorization or
  execution. Enforced by import-linter: *Character presentation never reaches
  authority* (no authority package imports the contract), *Presentation settings
  are a store, not an authority*, and *The activity scorer reads task rows and
  nothing else* (the scorer reaches storage and the shared schemas only — not the
  gateway, model layer, intelligence configuration, agent runtime, memory or
  vault). On Android, `CharacterBoundaryTest`.
- **Never the status.** The status indicator and its words remain the only
  account of what JARVIS is doing. The character is smoothed, delayed and
  decays by design, so it must not be read to learn a task's outcome; to
  accessibility services it is decorative.
- **Never about the user, never kept.** The scorer sees no task text or
  result, nothing the user said, no memory and no identity beyond whose rows
  these are. The report is computed per request, returned only to the
  caller's own clients, and never stored, cached, logged or sent to a model.
  The client keeps only the latest scores in memory until they go stale. No
  table, column or migration exists for it. History older than 30 minutes
  cannot influence anything (one timestamp within 24 h dates inactivity), so
  there is no longer-term profile to build.
- **Never a feeling.** No first-person statement, no mood that persists (the
  window decays; nothing outlives its evidence), no personality, nothing in a
  prompt. Inactivity is only ever `sleepy`; a user's cancellation is never
  `sad`; `angry` is load, never a reaction to the user.

## 8. Limitations

- **Impact is not measured.** The runtime records whether a task completed, not
  whether its result mattered. "Meaningful" work is approximated by "used a
  tool".
- **Polled, not pushed.** The face can lag an event by up to one poll interval
  (15 s), less when the current task changes on this device. A push channel for
  the report is possible later; it is not needed for correctness.
- **Worker switches are seen when their task ends** — the runtime keeps no
  switch time. A live `waiting_for_platform` task re-counts as a surprise if its
  row updates while it waits; `surprised`'s 60 s cooldown bounds that to once a
  minute at most.
- **Per user, not per device.** All of a user's devices see the same activity
  (a task from the phone shows on a future desktop companion too). That is
  intended — it is JARVIS's activity — but it means one device's work can wake
  another's character.
- **Constants are untuned.** Half-lives, saturation points and thresholds are
  reasoned starting values, chosen against the scenarios in §6, not fitted to
  real use. They change in one file, under tests.
- **No learned model** is involved on either side (none is permitted without a
  further decision).

## 9. Where it is tested

| What | Where |
|---|---|
| Scorer scenarios, semantics, determinism, bounds, what it can see | `tests/presentation/test_activity_scoring.py` |
| Policy vectors, wire samples, settings, invariants on random input | `tests/presentation/test_activity_policy.py`; Android `ActivityPolicyTest` |
| Endpoint: owner scope, no-store, writes nothing, carries no task content, token | `tests/runtime/test_presentation_activity_api.py`; route inventory |
| Intensity mapping | `tests/presentation/test_presentation_intensity.py`; Android `IntensityPolicyTest` |
| Android controller, runtime on virtual time, feed cadence, boundaries | `CharacterControllerTest`, `CharacterPresentationTest`, `CharacterActivityFeedTest`, `CharacterBoundaryTest` |
| Import boundaries | `lint-imports --config pyproject.toml` |

## 10. What OD-CHAR-4 retired

OD-CHAR-2's pipeline — a replaceable on-device `PresentationInferenceProvider`
scoring the current utterance (the deterministic English cue scorer
`LocalCueScorer`), the `PresentationInference` contract, the `select_expression`
selector with its confidence floor, and the `TaskCompleted` event bridge — is
deleted, with its tests and `presentation_selection_vectors.json`. One system
now selects the expression. The request text no longer reaches the character at
all, which removes the only user-content input the character ever had.
