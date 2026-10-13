"""The deterministic selection policy and its settings (OD-CHAR-2).

`shared/android/presentation_selection_vectors.json` is the cross-client
specification: the reference selector here and the Android selector
(`StateSelectorTest`) must both give every case's `expected` state.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from shared.schemas.presentation import (
    CharacterPresentationSettings,
    Expression,
    PresentationInference,
    select_expression,
)

ROOT = Path(__file__).resolve().parents[2]
VECTORS = json.loads((ROOT / "shared" / "android" / "presentation_selection_vectors.json").read_text())
DEFAULTS_FILE = ROOT / "assets" / "character" / "presentation.json"


def _settings(overrides: dict | None) -> CharacterPresentationSettings:
    base = CharacterPresentationSettings().model_dump(mode="json")
    for key, value in (overrides or {}).items():
        base[key] = {**base[key], **value} if isinstance(value, dict) else value
    return CharacterPresentationSettings.model_validate_json(json.dumps(base))


def _raw(result: object) -> str | None:
    if result is None or isinstance(result, str):
        return result
    return json.dumps(result)


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["name"])
def test_the_reference_selector_meets_every_shared_vector(case):
    now = datetime.fromisoformat(case["now"].replace("Z", "+00:00")) if "now" in case else None
    selected = select_expression(_raw(case["result"]), _settings(case.get("settings")), now)
    assert selected is Expression(case["expected"])


def test_the_vectors_cover_every_state_and_every_tie_break():
    expected = {case["expected"] for case in VECTORS["cases"]}
    assert expected == {e.value for e in Expression}
    ties = [c["name"] for c in VECTORS["cases"] if c["name"].startswith("tie:")]
    assert len(ties) >= 6


def test_the_shipped_default_settings_are_the_built_in_defaults():
    assert CharacterPresentationSettings.model_validate_json(DEFAULTS_FILE.read_text()) == (
        CharacterPresentationSettings()
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"thresholds": {"happy": 0}},
        {"thresholds": {"sad": 11}},
        {"thresholds": {"happy": 7.5}},
        {"thresholds": {"furious": 5}},
        {"min_confidence": 1.1},
        {"min_confidence": -0.1},
        {"timing": {"transition_ms": -1}},
        {"timing": {"transition_ms": 5000}},
        {"timing": {"idle_timeout_ms": 1000}},
        {"timing": {"interrupt_score": 0}},
        {"timing": {"hold_ms": {"surprised": 100}}},
        {"timing": {"hold_ms": {"default": 1000}}},  # the baseline has no hold
        {"reduced_motion": "sometimes"},
        {"transition": "spin"},
        {"schema_version": 2},
        {"expression": "happy"},  # settings never carry a state
    ],
)
def test_invalid_settings_are_rejected(overrides):
    with pytest.raises(ValidationError):
        _settings(overrides)


def test_a_typed_result_is_validated_whole():
    good = dict(
        happy_score=9,
        satisfied_score=1,
        default_score=1,
        sleepy_score=1,
        surprised_score=1,
        angry_score=1,
        sad_score=1,
        confidence=0.9,
        timestamp="2026-10-13T12:00:00Z",
        signals=["text", "event"],
        uncertain=False,
    )
    result = PresentationInference.model_validate_json(json.dumps(good))
    assert select_expression(result, CharacterPresentationSettings()) is Expression.HAPPY
    with pytest.raises(ValidationError):
        PresentationInference.model_validate_json(json.dumps({**good, "confidence": float("nan")}))
