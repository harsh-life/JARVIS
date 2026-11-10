"""The client activity policy, the activity report on the wire, and the
settings that parameterise them (OD-CHAR-4).

`shared/android/presentation_activity_policy_vectors.json` and
`presentation_activity_samples.json` are the cross-client specification: the
reference policy and parser here and the Android ones (`ActivityPolicyTest`,
`ActivityReportTest`) must give every case's expected result.
"""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path

import pytest
from pydantic import ValidationError

from shared.schemas.presentation import (
    TRANSIENT_STATES,
    ActivityPolicy,
    ActivityPresentation,
    CharacterPresentationSettings,
    Expression,
)

ROOT = Path(__file__).resolve().parents[2]
VECTORS = json.loads((ROOT / "shared/android/presentation_activity_policy_vectors.json").read_text())
SAMPLES = json.loads((ROOT / "shared/android/presentation_activity_samples.json").read_text())
DEFAULTS_FILE = ROOT / "assets/character/presentation.json"
BASE_SCORES = {"happy": 1, "satisfied": 1, "default": 5, "sleepy": 1, "surprised": 1, "angry": 1, "sad": 1}
COMPONENTS = dict.fromkeys(
    ("engagement", "productivity", "steadiness", "failure", "pressure", "surprise", "inactivity"), 0
)


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in extra.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def _settings(overrides: dict | None = None) -> CharacterPresentationSettings:
    doc = _merge(CharacterPresentationSettings().model_dump(mode="json"), overrides or {})
    return CharacterPresentationSettings.model_validate_json(json.dumps(doc))


def report(scores: dict | str) -> str:
    """A wire report: a score map over the base scores, or an invalid one."""

    if scores == "invalid":
        return json.dumps({"schema_version": 1, "computed_at": "2026-11-10T12:00:00Z", "window_s": 1800,
                           "scores": {**BASE_SCORES, "happy": 11}, "components": COMPONENTS})
    return json.dumps({"schema_version": 1, "computed_at": "2026-11-10T12:00:00Z", "window_s": 1800,
                       "scores": {**BASE_SCORES, **scores}, "components": COMPONENTS})


def _apply(policy: ActivityPolicy, step: dict) -> None:
    if "report" in step:
        policy.on_report(report(step["report"]), step["at"])
    elif step.get("activity"):
        policy.on_local_activity(step["at"])
    else:
        policy.tick(step["at"])


# --- the shared vectors -----------------------------------------------------------


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["name"])
def test_the_reference_policy_meets_every_shared_vector(case):
    policy = ActivityPolicy(_settings(case.get("settings")), 0)
    for i, step in enumerate(case["steps"]):
        _apply(policy, step)
        assert [policy.shown.value, policy.intensity] == step["expect"], f"step {i} at {step['at']}"


def test_the_vectors_reach_every_state_and_cover_the_required_behaviours():
    reached = {step["expect"][0] for case in VECTORS["cases"] for step in case["steps"]}
    assert reached == {e.value for e in Expression}
    names = " ".join(case["name"] for case in VECTORS["cases"])
    for behaviour in ("inactivity", "increasing interaction", "sustained workload", "recovers",
                      "failed task", "unexpected event", "cooldown", "hysteresis", "invalid",
                      "stale", "minimum display", "dead band", "tie:"):
        assert behaviour in names, behaviour


@pytest.mark.parametrize("sample", SAMPLES["valid"], ids=lambda s: s["name"])
def test_valid_wire_reports_are_accepted(sample):
    ActivityPresentation.model_validate_json(sample["text"])


@pytest.mark.parametrize("sample", SAMPLES["invalid"], ids=lambda s: s["name"])
def test_invalid_wire_reports_are_rejected_whole(sample):
    with pytest.raises(ValidationError):
        ActivityPresentation.model_validate_json(sample["text"])


@pytest.mark.parametrize("sample", SAMPLES["invalid"], ids=lambda s: s["name"])
def test_an_invalid_report_never_moves_the_character(sample):
    policy = ActivityPolicy(CharacterPresentationSettings(), 0)
    policy.on_report(report({"happy": 9}), 0)
    policy.on_report(sample["text"], 1_000)
    assert (policy.shown, policy.intensity) == (Expression.HAPPY, 8)


# --- properties beyond the vectors --------------------------------------------------


def _random_run(seed: int, settings: CharacterPresentationSettings):
    rng = random.Random(seed)
    policy = ActivityPolicy(settings, 0)
    now, changes = 0, []
    for _ in range(400):
        now += rng.choice([500, 1_000, 5_000, 15_000, 60_000])
        roll = rng.random()
        if roll < 0.7:
            scores = {e.value: rng.randint(1, 10) for e in Expression}
            policy.on_report(report(scores), now)
        elif roll < 0.8:
            policy.on_report("not a report", now)
        elif roll < 0.9:
            policy.on_local_activity(now)
        else:
            policy.tick(now)
        changes.append((now, policy.shown, policy.intensity))
    return changes


@pytest.mark.parametrize("seed", range(25))
def test_any_input_sequence_stays_bounded_and_respects_cooldowns(seed):
    settings = CharacterPresentationSettings()
    changes = _random_run(seed, settings)
    left: dict[Expression, int] = {}
    previous = Expression.DEFAULT
    for now, shown, intensity in changes:
        assert 1 <= intensity <= 10
        if shown is not previous:
            if previous is not Expression.DEFAULT:
                left[previous] = now
            if shown in left and shown is not Expression.SLEEPY:
                # Re-entry only after the cooldown (a baseline sleepy has none).
                assert now - left[shown] >= settings.activity.cooldown_ms.of(shown) or shown is Expression.DEFAULT
        previous = shown


def test_the_same_inputs_always_give_the_same_presentation():
    settings = CharacterPresentationSettings()
    assert _random_run(7, settings) == _random_run(7, settings)


def test_surprised_is_the_only_transient_state():
    assert TRANSIENT_STATES == {Expression.SURPRISED}


def test_next_change_at_names_the_staleness_and_the_surprise_hold():
    policy = ActivityPolicy(CharacterPresentationSettings(), 0)
    policy.on_report(report({"surprised": 10}), 1_000)
    assert policy.next_change_at(1_000) == 2_500           # min display (1 000 + 1 500)
    policy.tick(2_500)
    assert policy.next_change_at(2_500) == 3_500           # the surprise hold (1 000 + 2 500)
    policy.tick(3_500)
    assert policy.shown is Expression.DEFAULT
    assert policy.next_change_at(3_500) == 61_001          # the report goes stale


# --- settings ------------------------------------------------------------------------


def test_the_shipped_default_settings_are_the_built_in_defaults():
    assert CharacterPresentationSettings.model_validate_json(DEFAULTS_FILE.read_text()) == (
        CharacterPresentationSettings()
    )


def test_the_activity_block_is_optional():
    doc = CharacterPresentationSettings().model_dump(mode="json")
    del doc["activity"]
    assert CharacterPresentationSettings.model_validate_json(json.dumps(doc)) == CharacterPresentationSettings()


@pytest.mark.parametrize(
    "overrides",
    [
        {"thresholds": {"happy": 0}},
        {"thresholds": {"sad": 11}},
        {"thresholds": {"happy": 7.5}},
        {"thresholds": {"furious": 5}},
        {"min_confidence": 1.1},
        {"timing": {"transition_ms": -1}},
        {"timing": {"idle_timeout_ms": 1000}},
        {"timing": {"interrupt_score": 0}},
        {"timing": {"hold_ms": {"surprised": 100}}},
        {"timing": {"hold_ms": {"default": 1000}}},  # the baseline has no hold
        {"reduced_motion": "sometimes"},
        {"transition": "spin"},
        {"schema_version": 2},
        {"expression": "happy"},  # settings never carry a state
        {"activity": {"poll_interval_ms": 1000}},
        {"activity": {"max_age_ms": 700_000}},
        {"activity": {"poll_interval_ms": 60_000, "max_age_ms": 60_000}},  # must outlive a poll
        {"activity": {"exit_margin": 6}},
        {"activity": {"exit_margin": -1}},
        {"activity": {"cooldown_ms": {"angry": -1}}},
        {"activity": {"cooldown_ms": {"default": 1000}}},  # the baseline never cools down
        {"activity": {"mood_decay": 5}},
        {"activity": {"exit_margin": "2"}},
    ],
)
def test_invalid_settings_are_rejected(overrides):
    with pytest.raises(ValidationError):
        _settings(overrides)
