"""Expressive presentation character — the client-neutral contract (OD-CHAR-1…4).

The owner decisions of 2026-10-12/13 (`docs/DECISION_REGISTER.md` §2M, PRD §23
amendment) adopt the default character as JARVIS's expressive presentation;
OD-CHAR-4 (2026-11-10) makes it express JARVIS's *operational activity* — how
much work it is doing, how that work is going, how recently it was asked —
as computed by the server ("Central AI") from JARVIS's own task records. This
module is the one definition every client (Android today; web, tablet,
desktop and the configuration website later) shares:

* `Expression` — the closed vocabulary of presentation states, in the owner's
  order, which is also the deterministic tie-break order. A value outside it
  is never shown: it resolves to `DEFAULT`.
* `CharacterProfile` — which image shows each state (`assets/character/<id>/
  profile.json`). The mapping is data.
* `ActivityPresentation` — what the server's activity scorer returns
  (`GET /api/v1/presentation/activity`, `server/presentation/activity.py`): a
  1–10 score per state plus the 0–100 components it was computed from, so
  every score can be explained. A score is how well a state presents JARVIS's
  current operational activity — never a measurement of the user, and never
  a claim that JARVIS has feelings.
* `CharacterPresentationSettings` — provider-neutral presentation settings:
  visibility, profile, per-state activation thresholds, transition, timing,
  intensity and (optional) the activity policy's hysteresis, cooldowns and
  polling. Separate from the profile, so artwork mapping and behaviour change
  independently. Thresholds are product parameters, not validated
  measurements.
* `ActivityPolicy` — the reference implementation of the client's
  deterministic presentation policy: hysteresis, minimum display, cooldowns,
  staleness and the local wake rule, turning a stream of reports into one
  committed `(expression, intensity)`. Every client implements the same
  policy and is held to it by
  `shared/android/presentation_activity_policy_vectors.json`.
* `presentation_intensity` — how strongly the shown expression is presented
  (1–10), derived deterministically from its score and its state's threshold;
  the two baselines take fixed intensities when they have no score. Held to it
  by `shared/android/presentation_intensity_vectors.json`.

An expression is **presentation only**: it is not an agent mood that persists
(EMO-001: it is recomputed from a bounded, decaying window and keeps nothing),
not a trait system (EMO-003), never a first-person statement of feeling
(EMO-004), never stored as a memory or a profile of the user, and never the
account of what JARVIS is doing (the status indicator is). It grants nothing.
No authority package imports this module (import-linter contract "Character
presentation never reaches authority"), so no expression, score or intensity
can influence authentication, membership, capabilities, policy, risk tiers,
confirmation, budgets or tool execution. Deliberately **not** re-exported from
`shared.schemas`, so importing the data contracts never pulls it in.

OD-CHAR-4 retired the utterance-based inference contract
(`PresentationInference`, `select_expression`) and the Android cue scorer that
fed it: one system selects the expression.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

CHARACTER_SCHEMA_VERSION = 1
DEFAULT_PROFILE_ID = "default"

# A profile id is a directory name; an asset is a bare file name inside it.
# Neither can name a path (`/`, `..`) or anything but a PNG or WebP image.
PROFILE_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
ASSET_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}\.(png|webp)$"

SCORE_MIN = 1
SCORE_MAX = 10

INTENSITY_MIN = 1
INTENSITY_MAX = 10
# The canonical animation, exactly as authored (intensities 5 and 6 both present it).
INTENSITY_NEUTRAL = 5


class Expression(str, Enum):
    """The seven presentation states, in the owner's order (OD-CHAR-2, kept by OD-CHAR-4).

    The declaration order is the tie-break order of `ActivityPolicy`.
    `DEFAULT` is the initial and fallback state.
    """

    HAPPY = "happy"
    SATISFIED = "satisfied"
    DEFAULT = "default"
    SLEEPY = "sleepy"
    SURPRISED = "surprised"
    ANGRY = "angry"
    SAD = "sad"

    @classmethod
    def parse(cls, value: object) -> Expression:
        """A supplied state, validated: anything unknown is `DEFAULT`, never an error."""

        try:
            return cls(value)
        except (ValueError, TypeError):
            return cls.DEFAULT


class CharacterProfile(BaseModel):
    """State → image for one character. A state it omits shows `fallback`."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = CHARACTER_SCHEMA_VERSION
    profile_id: str = Field(pattern=PROFILE_ID_PATTERN)
    fallback: Expression = Expression.DEFAULT
    states: dict[Expression, str] = Field(min_length=1)

    @model_validator(mode="after")
    def _assets_are_bare_images_and_fallback_is_mapped(self) -> CharacterProfile:
        for state, asset in self.states.items():
            if not re.fullmatch(ASSET_NAME_PATTERN, asset):
                raise ValueError(f"asset for {state.value!r} is not a bare .png/.webp name")
        if self.fallback not in self.states:
            raise ValueError("the fallback state has no image")
        return self

    def asset_for(self, value: object) -> str:
        """The image for a supplied state; the fallback's for unknown or unmapped ones."""

        return self.states.get(Expression.parse(value), self.states[self.fallback])


# --- the activity report (server → client) -----------------------------------

ACTIVITY_SCHEMA_VERSION = 1
COMPONENT_MIN = 0
COMPONENT_MAX = 100

_Score = Field(ge=SCORE_MIN, le=SCORE_MAX)
_Component = Field(ge=COMPONENT_MIN, le=COMPONENT_MAX)


class ActivityScores(BaseModel):
    """A 1–10 score per state: how well that state presents JARVIS's current
    operational activity. Every state is always scored."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    happy: int = _Score
    satisfied: int = _Score
    default: int = _Score
    sleepy: int = _Score
    surprised: int = _Score
    angry: int = _Score
    sad: int = _Score

    def of(self, expression: Expression) -> int:
        return getattr(self, expression.value)


class ActivityComponents(BaseModel):
    """The measured components the scores are computed from, each 0–100 (a
    percentage of its saturation point). Sent so a score can be explained;
    they describe JARVIS's own work, not the user.

    * `engagement` — how often the user asked JARVIS for something recently;
    * `productivity` — recent completed work, weighted towards work that used tools;
    * `steadiness` — that recent work is completing at all;
    * `failure` — recent tasks that failed (never the user's own cancellations);
    * `pressure` — the strongest of: concurrent live tasks, recent model and
      tool calls, recent ceiling/rate-limit stops;
    * `surprise` — very recent unexpected operational events;
    * `inactivity` — how long JARVIS has had nothing to do.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    engagement: int = _Component
    productivity: int = _Component
    steadiness: int = _Component
    failure: int = _Component
    pressure: int = _Component
    surprise: int = _Component
    inactivity: int = _Component


class ActivityPresentation(BaseModel):
    """One activity report. Validated whole: one bad field rejects it.

    `computed_at` is the server's clock and is informational: a client judges
    staleness by when *it* received the report (its own monotonic clock), so
    a skewed phone clock cannot make a report look fresh or stale.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = ACTIVITY_SCHEMA_VERSION
    computed_at: AwareDatetime
    window_s: int = Field(ge=60, le=86_400)
    scores: ActivityScores
    components: ActivityComponents


# --- settings ---------------------------------------------------------------


class CharacterTransition(str, Enum):
    CROSSFADE = "crossfade"
    NONE = "none"


class ReducedMotion(str, Enum):
    """`system`: follow the device's animation setting; `always`: never animate."""

    SYSTEM = "system"
    ALWAYS = "always"


class ActivationThresholds(BaseModel):
    """Per-state activation threshold on the 1–10 score scale (product parameters)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    happy: int = Field(default=7, ge=SCORE_MIN, le=SCORE_MAX)
    satisfied: int = Field(default=7, ge=SCORE_MIN, le=SCORE_MAX)
    default: int = Field(default=1, ge=SCORE_MIN, le=SCORE_MAX)
    sleepy: int = Field(default=8, ge=SCORE_MIN, le=SCORE_MAX)
    surprised: int = Field(default=7, ge=SCORE_MIN, le=SCORE_MAX)
    angry: int = Field(default=8, ge=SCORE_MIN, le=SCORE_MAX)
    sad: int = Field(default=8, ge=SCORE_MIN, le=SCORE_MAX)

    def of(self, expression: Expression) -> int:
        return getattr(self, expression.value)


class HoldDurations(BaseModel):
    """Maximum display time (ms) per non-baseline state. Under the activity
    policy only the transient `surprised` uses it (it is an event, not a
    condition); the others stay while their evidence does, and their values
    are kept for document compatibility."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    happy: int = Field(default=5_000, ge=500, le=60_000)
    satisfied: int = Field(default=5_000, ge=500, le=60_000)
    sleepy: int = Field(default=5_000, ge=500, le=60_000)
    surprised: int = Field(default=2_500, ge=500, le=60_000)
    angry: int = Field(default=4_000, ge=500, le=60_000)
    sad: int = Field(default=5_000, ge=500, le=60_000)


class BaselineIntensities(BaseModel):
    """The fixed intensities of the two baseline states when there is no
    activity evidence to score them from (signed out, offline, stale)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    default: int = Field(default=INTENSITY_NEUTRAL, ge=INTENSITY_MIN, le=INTENSITY_MAX)
    sleepy: int = Field(default=2, ge=INTENSITY_MIN, le=INTENSITY_MAX)

    def of(self, expression: Expression) -> int:
        if expression is Expression.DEFAULT:
            return self.default
        if expression is Expression.SLEEPY:
            return self.sleepy
        raise ValueError("not a baseline state")


class IntensitySettings(BaseModel):
    """Parameters of `presentation_intensity` (product parameters, like the
    thresholds). Optional in a document: absent, these defaults apply."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # The intensity of a selection that only just meets its state's threshold.
    floor: int = Field(default=4, ge=INTENSITY_MIN, le=INTENSITY_MAX)
    # The shown state selected again changes intensity only by at least this.
    dead_band: int = Field(default=2, ge=0, le=9)
    baseline: BaselineIntensities = BaselineIntensities()


class PresentationTiming(BaseModel):
    """Transition, minimum display and local idle policy (ms)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    transition_ms: int = Field(default=220, ge=0, le=2_000)
    min_display_ms: int = Field(default=1_500, ge=0, le=30_000)
    hold_ms: HoldDurations = HoldDurations()
    idle_timeout_ms: int = Field(default=300_000, ge=10_000, le=86_400_000)
    # A state scoring at least this replaces the shown one without waiting
    # for its minimum display time, and retargets a transition in progress.
    interrupt_score: int = Field(default=9, ge=SCORE_MIN, le=SCORE_MAX)
    # Unused since OD-CHAR-4 (the activity report's age is
    # `activity.max_age_ms`); accepted so stored documents stay valid.
    max_result_age_ms: int = Field(default=5_000, ge=100, le=60_000)


class ActivityCooldowns(BaseModel):
    """After a non-default state leaves the screen, how long (ms) before it may
    be entered again — so an expression cannot flicker back and forth, and an
    unexpected event cannot produce `surprised` over and over."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    happy: int = Field(default=30_000, ge=0, le=600_000)
    satisfied: int = Field(default=15_000, ge=0, le=600_000)
    sleepy: int = Field(default=30_000, ge=0, le=600_000)
    surprised: int = Field(default=60_000, ge=0, le=600_000)
    angry: int = Field(default=60_000, ge=0, le=600_000)
    sad: int = Field(default=30_000, ge=0, le=600_000)

    def of(self, expression: Expression) -> int:
        if expression is Expression.DEFAULT:
            return 0
        return getattr(self, expression.value)


class ActivitySettings(BaseModel):
    """The activity policy's own parameters (OD-CHAR-4). Optional in a
    document: absent, these defaults apply, so every stored document stays
    valid."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    # How often a client asks for a fresh report while it is in front.
    poll_interval_ms: int = Field(default=15_000, ge=5_000, le=300_000)
    # A report older than this (by the client's own clock, from receipt) is
    # no longer evidence: the character falls back to its baseline.
    max_age_ms: int = Field(default=60_000, ge=10_000, le=600_000)
    # Hysteresis: the shown state stays while its score is at least its
    # threshold minus this margin; it is entered only at the threshold.
    exit_margin: int = Field(default=2, ge=0, le=5)
    cooldown_ms: ActivityCooldowns = ActivityCooldowns()

    @model_validator(mode="after")
    def _a_report_outlives_one_poll(self) -> ActivitySettings:
        if self.max_age_ms <= self.poll_interval_ms:
            raise ValueError("max_age_ms must exceed poll_interval_ms")
        return self


class CharacterPresentationSettings(BaseModel):
    """How a client presents and selects the character — independent of the
    profile's mapping.

    Under the activity policy (OD-CHAR-4), `min_confidence`,
    `timing.max_result_age_ms` and the `hold_ms` of every state but
    `surprised` are accepted for document compatibility and not used: activity
    reports carry no confidence, their age is `activity.max_age_ms`, and only
    the transient `surprised` has a maximum display time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = CHARACTER_SCHEMA_VERSION
    visible: bool = True
    profile_id: str = Field(default=DEFAULT_PROFILE_ID, pattern=PROFILE_ID_PATTERN)
    transition: CharacterTransition = CharacterTransition.CROSSFADE
    reduced_motion: ReducedMotion = ReducedMotion.SYSTEM
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0, allow_inf_nan=False)
    thresholds: ActivationThresholds = ActivationThresholds()
    timing: PresentationTiming = PresentationTiming()
    intensity: IntensitySettings = IntensitySettings()
    activity: ActivitySettings = ActivitySettings()


# --- the intensity policy ---------------------------------------------------


def presentation_intensity(score: int, threshold: int, floor: int) -> int:
    """How strongly a selected expression is presented (1–10), from its
    suitability `score` and its state's `threshold` — the same mapping for
    every state:

        floor + round_half_up((score - threshold) * (10 - floor) / (10 - threshold))

    A score at the threshold presents at `floor`, a score of 10 at 10; a
    threshold of 10 admits only 10, which presents at 10. Integer arithmetic
    only. Raises `ValueError` for a value out of range or a score below its
    threshold (the selector never selects one).
    """

    for name, value, low, high in (
        ("score", score, SCORE_MIN, SCORE_MAX),
        ("threshold", threshold, SCORE_MIN, SCORE_MAX),
        ("floor", floor, INTENSITY_MIN, INTENSITY_MAX),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{name} out of range")
    if score < threshold:
        raise ValueError("the score does not meet its threshold")
    if threshold == SCORE_MAX:
        return INTENSITY_MAX
    span = SCORE_MAX - threshold
    numerator = (score - threshold) * (INTENSITY_MAX - floor)
    return floor + (2 * numerator + span) // (2 * span)


def baseline_intensity(expression: Expression, settings: CharacterPresentationSettings) -> int:
    """The fixed intensity of a baseline state (`DEFAULT` or `SLEEPY`); no
    score is involved."""

    return settings.intensity.baseline.of(expression)


def retained_intensity(current: int, candidate: int, dead_band: int) -> int:
    """The shown state selected again: the candidate intensity replaces the
    current one only if it differs by at least `dead_band`."""

    return candidate if abs(candidate - current) >= dead_band else current


# --- the activity policy (client side) ----------------------------------------

# States with a maximum display time (`timing.hold_ms`): an event, not a
# condition. Every other state stays as long as its evidence does.
TRANSIENT_STATES = frozenset({Expression.SURPRISED})


@dataclass
class ActivityPolicy:
    """The deterministic presentation policy every client runs (OD-CHAR-4).

    Pure and clock-free: every call takes `now_ms`, a monotonic millisecond
    clock. In memory only — nothing here is stored, logged or sent. Rules, at
    each report, local activity or tick:

    1. **Evidence.** The last valid report is evidence until
       `activity.max_age_ms` after it was *received*. An invalid report is
       discarded and changes nothing; it is not evidence of anything.
    2. **No evidence** (signed out, offline, stale): the baseline only —
       `default`, or `sleepy` once there has been no local activity for
       `timing.idle_timeout_ms` — at the baselines' fixed intensities.
    3. **Local wake.** Local activity (opening the app, starting voice input,
       submitting a request) within `idle_timeout_ms` makes `sleepy`
       ineligible, whatever the report says: a user who is here is answered.
    4. **Enter.** A non-default state may be entered only when its score
       reaches its threshold and it is not cooling down
       (`activity.cooldown_ms`, from when it last left the screen).
    5. **Hysteresis.** The shown state stays while its score is at least its
       threshold minus `activity.exit_margin` — except `surprised`, which
       leaves after `timing.hold_ms.surprised`.
    6. **Choice.** The highest-scoring eligible state is chosen; the shown
       state wins a tie, and otherwise ties go to the earlier state in the
       owner's order. Nothing eligible → `default`.
    7. **Minimum display.** While the shown state still holds, a different
       choice replaces it only after `timing.min_display_ms`, unless the
       choice scores at least `timing.interrupt_score`. A state that no
       longer holds leaves at once.
    8. **Intensity.** A state with a score presents at
       `presentation_intensity(max(score, threshold), threshold, floor)` —
       held between exit and entry it stays at the floor; a baseline without
       evidence at its fixed intensity. The shown state keeps its intensity
       unless the new one differs by at least `intensity.dead_band`.
    """

    settings: CharacterPresentationSettings
    now_ms: int
    shown: Expression = field(init=False, default=Expression.DEFAULT)
    intensity: int = field(init=False, default=INTENSITY_NEUTRAL)
    since_ms: int = field(init=False, default=0)
    _scores: ActivityScores | None = field(init=False, default=None)
    _received_ms: int | None = field(init=False, default=None)
    _local_ms: int = field(init=False, default=0)
    _last_exit: dict[Expression, int] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        # Starting the app is local activity, and the character starts at the baseline.
        self.since_ms = self.now_ms
        self._local_ms = self.now_ms
        self.intensity = baseline_intensity(Expression.DEFAULT, self.settings)

    # inputs ---------------------------------------------------------------

    def on_report(self, report: ActivityPresentation | str | bytes | None, now_ms: int) -> None:
        if isinstance(report, (str, bytes)):
            try:
                report = ActivityPresentation.model_validate_json(report)
            except ValidationError:
                report = None
        if report is not None:
            self._scores = report.scores
            self._received_ms = now_ms
        self._evaluate(now_ms)

    def on_local_activity(self, now_ms: int) -> None:
        self._local_ms = now_ms
        self._evaluate(now_ms)

    def tick(self, now_ms: int) -> None:
        self._evaluate(now_ms)

    def next_change_at(self, now_ms: int) -> int | None:
        """The earliest time a tick could change something without new input."""

        s = self.settings
        times = [self._local_ms + s.timing.idle_timeout_ms]
        if self._received_ms is not None:
            times.append(self._received_ms + s.activity.max_age_ms + 1)
        if self.shown is not Expression.DEFAULT:
            times.append(self.since_ms + s.timing.min_display_ms)
        if self.shown in TRANSIENT_STATES:
            times.append(self.since_ms + self._hold(self.shown))
        times += [left + s.activity.cooldown_ms.of(e) for e, left in self._last_exit.items()]
        future = [t for t in times if t > now_ms]
        return min(future) if future else None

    # the policy -----------------------------------------------------------

    def _hold(self, expression: Expression) -> int:
        return getattr(self.settings.timing.hold_ms, expression.value)

    def _evaluate(self, now: int) -> None:
        s = self.settings
        fresh = self._received_ms is not None and now - self._received_ms <= s.activity.max_age_ms
        if not fresh or self._scores is None:
            idle = now - self._local_ms >= s.timing.idle_timeout_ms
            target = Expression.SLEEPY if idle else Expression.DEFAULT
            self._commit(target, baseline_intensity(target, s), now)
            return

        scores = {e: self._scores.of(e) for e in Expression}
        if now - self._local_ms < s.timing.idle_timeout_ms:
            scores[Expression.SLEEPY] = SCORE_MIN
        threshold = s.thresholds.of

        def cooling(e: Expression) -> bool:
            left = self._last_exit.get(e)
            return left is not None and now - left < s.activity.cooldown_ms.of(e)

        shown = self.shown
        holds = (
            shown is not Expression.DEFAULT
            and scores[shown] >= threshold(shown) - s.activity.exit_margin
            and not (shown in TRANSIENT_STATES and now - self.since_ms >= self._hold(shown))
        )
        best = shown if holds else None
        for e in Expression:  # declaration order = tie-break order
            if e is Expression.DEFAULT or e is shown:
                continue
            if scores[e] < threshold(e) or cooling(e):
                continue
            if best is None or scores[e] > scores[best]:
                best = e
        target = best or Expression.DEFAULT

        if target is not shown and holds:
            settled = now - self.since_ms >= s.timing.min_display_ms
            if not settled and scores[target] < s.timing.interrupt_score:
                target = shown
        self._commit(target, self._intensity_for(target, scores), now)

    def _intensity_for(self, e: Expression, scores: dict[Expression, int]) -> int:
        if e is Expression.DEFAULT:
            return baseline_intensity(e, self.settings)
        threshold = self.settings.thresholds.of(e)
        return presentation_intensity(max(scores[e], threshold), threshold, self.settings.intensity.floor)

    def _commit(self, target: Expression, intensity: int, now: int) -> None:
        if target is self.shown:
            self.intensity = retained_intensity(self.intensity, intensity, self.settings.intensity.dead_band)
            return
        if self.shown is not Expression.DEFAULT:
            self._last_exit[self.shown] = now
        self.shown = target
        self.intensity = intensity
        self.since_ms = now
