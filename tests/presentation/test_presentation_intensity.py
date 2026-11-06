"""The deterministic presentation-intensity policy and its settings.

`shared/android/presentation_intensity_vectors.json` is the cross-client
specification: the reference implementation here and the Android policy
(`IntensityPolicyTest`) must both give every case's `expected` value.
Intensity is how strongly the *selected* expression is presented; it is
neither the suitability score nor the confidence, and nothing that
authorizes or executes reads it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from shared.schemas.presentation import (
    CharacterPresentationSettings,
    Expression,
    baseline_intensity,
    presentation_intensity,
    retained_intensity,
)

ROOT = Path(__file__).resolve().parents[2]
VECTORS = json.loads((ROOT / "shared" / "android" / "presentation_intensity_vectors.json").read_text())
DEFAULTS_FILE = ROOT / "assets" / "character" / "presentation.json"


@pytest.mark.parametrize("case", VECTORS["mapping"], ids=lambda c: c["name"])
def test_the_reference_policy_meets_every_shared_mapping_vector(case):
    if case["expected"] is None:
        with pytest.raises(ValueError):
            presentation_intensity(case["score"], case["threshold"], case["floor"])
    else:
        assert presentation_intensity(case["score"], case["threshold"], case["floor"]) == case["expected"]


def test_the_owners_table_at_the_common_floor():
    settings = CharacterPresentationSettings()
    assert settings.intensity.floor == 4
    table = {(7, 7): 4, (8, 7): 6, (9, 7): 8, (10, 7): 10, (8, 8): 4, (9, 8): 7, (10, 8): 10}
    for (score, threshold), expected in table.items():
        assert presentation_intensity(score, threshold, settings.intensity.floor) == expected
    with pytest.raises(ValueError):
        presentation_intensity(7, 8, 4)
    assert presentation_intensity(10, 10, 4) == 10


def test_the_vectors_cover_the_shipped_thresholds_and_every_rejection():
    settings = CharacterPresentationSettings()
    covered = {(c["score"], c["threshold"]) for c in VECTORS["mapping"] if c["floor"] == settings.intensity.floor}
    for expression in Expression:
        threshold = settings.thresholds.of(expression)
        assert all((score, threshold) in covered for score in range(threshold, 11))
    assert sum(1 for c in VECTORS["mapping"] if c["expected"] is None) >= 8


@pytest.mark.parametrize("case", VECTORS["baseline"], ids=lambda c: c["state"])
def test_the_baselines_meet_the_shared_vectors(case):
    assert baseline_intensity(Expression(case["state"]), CharacterPresentationSettings()) == case["expected"]


def test_only_the_baseline_states_have_a_fixed_intensity():
    with pytest.raises(ValueError):
        baseline_intensity(Expression.HAPPY, CharacterPresentationSettings())


@pytest.mark.parametrize("case", VECTORS["dead_band"], ids=lambda c: f"{c['current']}->{c['candidate']}/{c['dead_band']}")
def test_the_dead_band_meets_the_shared_vectors(case):
    assert retained_intensity(case["current"], case["candidate"], case["dead_band"]) == case["expected"]


def test_values_that_are_not_integers_are_rejected():
    for bad in (True, 7.0, "7", None):
        with pytest.raises(ValueError):
            presentation_intensity(bad, 7, 4)  # type: ignore[arg-type]


def test_the_intensity_block_is_optional_and_validated_whole():
    base = CharacterPresentationSettings().model_dump(mode="json")
    without = {k: v for k, v in base.items() if k != "intensity"}
    assert CharacterPresentationSettings.model_validate_json(json.dumps(without)) == CharacterPresentationSettings()
    tuned = {**base, "intensity": {"floor": 3, "dead_band": 1, "baseline": {"default": 5, "sleepy": 3}}}
    settings = CharacterPresentationSettings.model_validate_json(json.dumps(tuned))
    assert settings.intensity.floor == 3
    assert baseline_intensity(Expression.SLEEPY, settings) == 3
    for bad in (
        {"floor": 0},
        {"floor": 11},
        {"floor": 4.5},
        {"dead_band": -1},
        {"dead_band": 10},
        {"baseline": {"happy": 5}},
        {"baseline": {"sleepy": 0}},
        {"gain": 2},
    ):
        with pytest.raises(ValidationError):
            CharacterPresentationSettings.model_validate_json(json.dumps({**base, "intensity": bad}))


def test_the_shipped_settings_carry_the_intensity_defaults():
    shipped = json.loads(DEFAULTS_FILE.read_text())
    assert shipped["intensity"] == CharacterPresentationSettings().model_dump(mode="json")["intensity"]
    assert shipped["schema_version"] == 1


def test_a_threshold_7_state_at_score_7_then_8_moves_from_4_to_6():
    """The dead band is "at least": 4 -> 6 is exactly 2 apart, so it moves."""

    first = presentation_intensity(7, 7, 4)
    second = presentation_intensity(8, 7, 4)
    assert (first, second) == (4, 6)
    assert retained_intensity(first, second, 2) == 6
    # With the default floor every score step of a threshold-7 or -8 state moves at least 2.
    for threshold in (7, 8):
        steps = [presentation_intensity(s, threshold, 4) for s in range(threshold, 11)]
        assert all(b - a >= 2 for a, b in zip(steps, steps[1:])), steps
