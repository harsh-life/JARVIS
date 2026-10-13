# Character presentation — how it works and what can be configured

Owner decisions **OD-CHAR-1** (the default character) and **OD-CHAR-2**
(selection from the current interaction): `docs/DECISION_REGISTER.md` §2M,
PRD §23 amendments. This page is the reference for client implementers and
for the future configuration website.

The character is an expressive interface. Its expression is a UI choice, not
evidence that JARVIS has feelings, and a score is an estimate that a state is
an appropriate *presentation* — not a measurement of anyone's emotions.

## 1. Six separate parts

```
 current request ─┐                                     ┌─ idle timer, activity
 audio features ──┤                                     │
 last ≤3 turns ───┼─▶ inference provider ─▶ selector ─▶ controller ─▶ renderer ─▶ image
 verified events ─┘   (replaceable)        (pure)       (pure, timed)   (Compose)    (profile)

 operational status ─────────────────────────────────────────────▶ status indicator (unchanged)
```

| Part | What it is | Android | Contract |
|---|---|---|---|
| Asset profile | which image shows each state | `CharacterProfile`, `CharacterAssets` | `CharacterProfile`, `assets/character/<id>/profile.json` |
| Inference provider | scores the 7 states for the current interaction | `PresentationInferenceProvider`; pilot: `LocalCueScorer` | `PresentationInference` |
| Selector | validates a result and picks a state, deterministically | `StateSelector` | `select_expression` + `shared/android/presentation_selection_vectors.json` |
| Controller | when to show, hold, return to baseline, sleep and wake | `CharacterController`, run by `CharacterPresentation` | `PresentationTiming` |
| Renderer | draws the chosen image; cross-fade or instant swap | `presentation/ui/Character.kt` | `transition`, `transition_ms`, `reduced_motion` |
| Operational status | what JARVIS is actually doing | `Presenter` → `StatusIndicator` (untouched) | — |

None of these is an authority. No server authority package imports the
contract (import-linter: *Character presentation never reaches authority*),
and on Android the character package imports nothing outside itself; the only
task signal it receives — a fresh, server-confirmed completion — comes through
`CharacterEvents`, which only reads the task tracker.

## 2. The selection policy

A provider returns `happy_score` … `sad_score` (integers 1–10), `confidence`
(0–1), `timestamp` (with a zone), `signals` (`text`, `transcript`, `prosody`,
`context`, `event`; no repeats) and `uncertain`. Then:

1. validate the result whole — one bad field rejects it;
2. a malformed, missing, stale (older than `max_result_age_ms`) or
   future-dated result → `default`;
3. `uncertain`, or `confidence < min_confidence` → `default`;
4. a state is eligible only if its score ≥ its own threshold;
5. the highest-scoring eligible state wins;
6. ties go to the earlier state in the owner's order: **happy, satisfied,
   default, sleepy, surprised, angry, sad**;
7. nothing eligible → `default`.

A `default` selection changes nothing on screen (insufficient evidence); the
baseline returns by itself when the shown state's hold ends.

## 3. Timing and idle policy (the controller)

- **Baseline:** `default`; `sleepy` after `idle_timeout_ms` without activity.
  The app starts at `default`.
- **Activity** (a submitted request, starting voice input, bringing the app
  to the front) restarts the idle timer and wakes `sleepy` at once.
- A **selected** state shows, holds for `hold_ms[state]`, then the baseline
  returns. No selected state stays indefinitely; `surprised` is brief.
- **Minimum display:** a selected state is not replaced sooner than
  `min_display_ms`; meanwhile only the latest candidate is kept, so rapid
  predictions produce at most one change. Leaving the baseline is immediate.
- **Interrupt:** a selection scoring ≥ `interrupt_score` replaces the shown
  state at once; the renderer retargets a cross-fade already in progress.
- **Reduced motion:** with `reduced_motion: always`, or `system` while the
  device has animations off, images swap without a fade.

## 4. What the configuration website will configure

Two independent documents, both validated by `shared/schemas/presentation.py`
on the server and by the same rules on every client (an invalid document is
rejected whole and the client keeps its built-in defaults):

**Presentation settings** (`CharacterPresentationSettings`; defaults in
`assets/character/presentation.json`):

| Field | Default | Range |
|---|---|---|
| `visible` | `true` | bool |
| `profile_id` | `"default"` | `[a-z0-9][a-z0-9_-]{0,63}` |
| `transition` | `"crossfade"` | `crossfade` \| `none` |
| `reduced_motion` | `"system"` | `system` \| `always` |
| `min_confidence` | `0.6` | 0–1 |
| `thresholds.<state>` | happy 7, satisfied 7, default 1, sleepy 8, surprised 7, angry 8, sad 8 | 1–10 |
| `timing.transition_ms` | 220 | 0–2000 |
| `timing.min_display_ms` | 1500 | 0–30000 |
| `timing.hold_ms.<state>` | happy 5000, satisfied 5000, sleepy 5000, surprised 2500, angry 4000, sad 5000 | 500–60000 (no `default`) |
| `timing.idle_timeout_ms` | 300000 | 10000–86400000 |
| `timing.interrupt_score` | 9 | 1–10 |
| `timing.max_result_age_ms` | 5000 | 100–60000 |

**Asset profile** (`CharacterProfile`): `profile_id`, `fallback`, and a
`states` map from state to a bare `.png`/`.webp` file name in that profile's
directory.

These thresholds are product parameters to be tuned on real use, not
scientifically validated cut-offs.

**Server store (pilot plan C10, 2026-10-13).** `GET/PUT/DELETE
/api/v1/config/presentation` keeps one validated `CharacterPresentationSettings`
per user, so every client of that user can render the same way
(`server/presentation`, table `presentation_settings`). It accepts exactly
this document — a document with any unknown field, out-of-range value or path
for a profile is refused whole — and nothing on the server reads it to decide
anything. Clients still ship `presentation.json` as their defaults; the
Android client does not fetch the stored settings yet. Asset profiles stay
client-side; the server never reads artwork.

## 5. The provider today, and a future model

The pilot provider, `LocalCueScorer`, is **deterministic and not trained**: a
short English cue list (with negation) plus verified events. It runs on the
device with no dependency or download. Its limits: English only, no sarcasm or
context understanding, no audio features yet (the Android recognizer does not
expose them), and `sleepy` only from the idle policy.

A small learned model (for example a quantised on-device text classifier,
optionally with prosody features) can replace it by implementing
`PresentationInferenceProvider` and emitting the `PresentationInference`
shape; `InferenceResult.parse` already validates such raw output. Fine-tuning
it needs consented, labelled examples: collection is **opt-in only**, keeps no
raw audio by default, and is a separate owner decision when it is built. No
model, dataset or training pipeline is part of this change.
