"""Expressive presentation character — the client-neutral contract (OD-CHAR-1, OD-CHAR-2).

The owner decisions of 2026-10-12/13 (`docs/DECISION_REGISTER.md` §2M, PRD §23
amendment) adopt the default character as JARVIS's expressive presentation and
let it be selected from the *current* interaction and verified application
events. This module is the one definition every client (Android today; web,
tablet, desktop and the configuration website later) shares:

* `Expression` — the closed vocabulary of presentation states, in the owner's
  order, which is also the deterministic tie-break order. A value outside it
  is never shown: it resolves to `DEFAULT`.
* `CharacterProfile` — which image shows each state (`assets/character/<id>/
  profile.json`). The mapping is data.
* `PresentationInference` — what an inference provider (any vendor, any
  library, or none) returns: a 1–10 score per state, a confidence, a
  timestamp, the signal types it used and an uncertainty flag. Scores are a
  provider's estimate that a state is an appropriate *presentation* for the
  current interaction — not a measurement of anyone's emotions.
* `CharacterPresentationSettings` — provider-neutral presentation settings:
  visibility, profile, per-state activation thresholds, the confidence
  floor, transition and timing/idle policy. Separate from the profile, so
  artwork mapping and behaviour change independently. The thresholds are
  product parameters to be tuned empirically, not validated measurements.
* `select_expression` — the reference implementation of the deterministic
  selection policy. Every client implements the same policy and is held to
  it by `shared/android/presentation_selection_vectors.json`.

An expression is **presentation only**: it is not an agent mood (EMO-001), not
a trait system (EMO-003), never a first-person statement of feeling (EMO-004),
never stored as a memory or a profile of the user, and never an operational
status. It grants nothing. No authority package imports this module
(import-linter contract "Character presentation never reaches authority"), so
no expression or score can influence authentication, membership, capabilities,
policy, risk tiers, confirmation, budgets or tool execution. Deliberately
**not** re-exported from `shared.schemas`, so importing the data contracts
never pulls it in.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
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


class Expression(str, Enum):
    """The seven presentation states, in the owner's order (OD-CHAR-2).

    The declaration order is the tie-break order of `select_expression`.
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


# --- inference --------------------------------------------------------------


class SignalType(str, Enum):
    """What a provider based its scores on. Never raw audio: `prosody` means
    derived features (e.g. loudness, pitch movement), and audio itself is not
    part of this contract."""

    TEXT = "text"  # the current typed request
    TRANSCRIPT = "transcript"  # the current spoken request, as text
    PROSODY = "prosody"  # features of the current utterance's audio
    CONTEXT = "context"  # the last few turns of the current session, in memory
    EVENT = "event"  # a verified application event (e.g. a task completed)


_Score = Field(ge=SCORE_MIN, le=SCORE_MAX)


class PresentationInference(BaseModel):
    """One provider result. Validated whole: one bad field rejects it."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    happy_score: int = _Score
    satisfied_score: int = _Score
    default_score: int = _Score
    sleepy_score: int = _Score
    surprised_score: int = _Score
    angry_score: int = _Score
    sad_score: int = _Score
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    timestamp: AwareDatetime
    signals: list[SignalType] = Field(max_length=len(SignalType))
    uncertain: bool

    @model_validator(mode="after")
    def _signals_are_distinct(self) -> PresentationInference:
        if len(set(self.signals)) != len(self.signals):
            raise ValueError("signal types repeat")
        return self

    def score(self, expression: Expression) -> int:
        return getattr(self, f"{expression.value}_score")


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
    """How long (ms) a selected, non-baseline state stays before the
    character returns to its baseline. Transient states never stay
    indefinitely."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    happy: int = Field(default=5_000, ge=500, le=60_000)
    satisfied: int = Field(default=5_000, ge=500, le=60_000)
    sleepy: int = Field(default=5_000, ge=500, le=60_000)
    surprised: int = Field(default=2_500, ge=500, le=60_000)
    angry: int = Field(default=4_000, ge=500, le=60_000)
    sad: int = Field(default=5_000, ge=500, le=60_000)


class PresentationTiming(BaseModel):
    """Transition and idle/reset policy (ms)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    transition_ms: int = Field(default=220, ge=0, le=2_000)
    min_display_ms: int = Field(default=1_500, ge=0, le=30_000)
    hold_ms: HoldDurations = HoldDurations()
    idle_timeout_ms: int = Field(default=300_000, ge=10_000, le=86_400_000)
    # A selected state scoring at least this interrupts the minimum display
    # time and any transition in progress.
    interrupt_score: int = Field(default=9, ge=SCORE_MIN, le=SCORE_MAX)
    # A result older than this (by its own timestamp) is stale and rejected.
    max_result_age_ms: int = Field(default=5_000, ge=100, le=60_000)


class CharacterPresentationSettings(BaseModel):
    """How a client presents and selects the character — independent of the
    profile's mapping and of whichever inference provider is used."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = CHARACTER_SCHEMA_VERSION
    visible: bool = True
    profile_id: str = Field(default=DEFAULT_PROFILE_ID, pattern=PROFILE_ID_PATTERN)
    transition: CharacterTransition = CharacterTransition.CROSSFADE
    reduced_motion: ReducedMotion = ReducedMotion.SYSTEM
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0, allow_inf_nan=False)
    thresholds: ActivationThresholds = ActivationThresholds()
    timing: PresentationTiming = PresentationTiming()


# --- the selection policy ---------------------------------------------------


def select_expression(
    result: PresentationInference | str | bytes | None,
    settings: CharacterPresentationSettings,
    now: datetime | None = None,
) -> Expression:
    """The deterministic selection policy (OD-CHAR-2), in order:

    1. validate the result (a raw provider output is parsed strictly);
    2. a malformed, missing or stale result selects `DEFAULT`;
    3. an uncertain result, or one below `min_confidence`, selects `DEFAULT`;
    4. a state is eligible only if its score reaches its own threshold;
    5. the highest-scoring eligible state is selected;
    6. ties go to the earlier state in `Expression`'s order;
    7. with no eligible state, `DEFAULT`.
    """

    if isinstance(result, (str, bytes)):
        try:
            result = PresentationInference.model_validate_json(result)
        except ValidationError:
            return Expression.DEFAULT
    if result is None:
        return Expression.DEFAULT
    if now is not None:
        age = now - result.timestamp
        if age > timedelta(milliseconds=settings.timing.max_result_age_ms) or age < -timedelta(seconds=1):
            return Expression.DEFAULT
    if result.uncertain or result.confidence < settings.min_confidence:
        return Expression.DEFAULT
    best: Expression | None = None
    for expression in Expression:  # declaration order = tie-break order
        score = result.score(expression)
        if score < settings.thresholds.of(expression):
            continue
        if best is None or score > result.score(best):
            best = expression
    return best or Expression.DEFAULT
