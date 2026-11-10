"""The Python reference and the JavaScript activity policy agree (OD-CHAR-4,
OD-DESK-1).

Both are held to the shared vectors; this goes further and replays random
input sequences — valid reports, malformed and near-valid reports (fractional,
quoted, out of range, missing fields, bad timestamps), local activity and
ticks, under several settings — through `ActivityPolicy` here and
`shared/web/activity-policy.js` under Node, and requires the same
presentation after every step.

Node is required in CI (`HYPERMIND_REQUIRE_NODE=1`): there the test fails
rather than skips if it is missing.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from shared.schemas.presentation import ActivityPolicy, Expression
from tests.presentation.test_activity_policy import _settings, report

ROOT = Path(__file__).resolve().parents[2]
TRACE = ROOT / "shared" / "web" / "tools" / "policy-trace.mjs"
SETTINGS = [
    {},
    {"activity": {"exit_margin": 0}},
    {"thresholds": {"happy": 9, "sad": 6}, "intensity": {"floor": 6, "dead_band": 0}},
    {"timing": {"min_display_ms": 0, "interrupt_score": 10}, "activity": {"cooldown_ms": {"surprised": 0}}},
    {"timing": {"idle_timeout_ms": 10_000}, "activity": {"poll_interval_ms": 5_000, "max_age_ms": 10_000}},
]


def _mangled(rng: random.Random) -> str:
    doc = json.loads(report({e.value: rng.randint(1, 10) for e in Expression}))
    choice = rng.randrange(9)
    if choice == 0:
        doc["scores"]["happy"] = 7.5
    elif choice == 1:
        doc["scores"]["sad"] = str(doc["scores"]["sad"])
    elif choice == 2:
        doc["scores"]["angry"] = 11
    elif choice == 3:
        del doc["components"]["pressure"]
    elif choice == 4:
        doc["computed_at"] = "2026-11-10T12:00:00"
    elif choice == 5:
        doc["extra"] = 1
    elif choice == 6:
        doc["window_s"] = 59
    elif choice == 7:
        doc["scores"]["happy"] = True
    else:
        return "{not json"
    return json.dumps(doc)


def _runs(seed: int) -> list[dict]:
    rng = random.Random(seed)
    runs = []
    for overrides in SETTINGS:
        now, steps = 0, []
        for _ in range(250):
            now += rng.choice([0, 200, 1_000, 1_500, 2_500, 5_000, 15_000, 60_000, 300_000])
            roll = rng.random()
            if roll < 0.6:
                steps.append({"at": now, "report": report({e.value: rng.randint(1, 10) for e in Expression})})
            elif roll < 0.75:
                steps.append({"at": now, "report": _mangled(rng)})
            elif roll < 0.87:
                steps.append({"at": now, "activity": True})
            else:
                steps.append({"at": now, "tick": True})
        runs.append({"settings": overrides, "steps": steps})
    return runs


def _python(run: dict) -> list[list]:
    policy = ActivityPolicy(_settings(run["settings"]), 0)
    out = []
    for step in run["steps"]:
        if "report" in step:
            policy.on_report(step["report"], step["at"])
        elif step.get("activity"):
            policy.on_local_activity(step["at"])
        else:
            policy.tick(step["at"])
        out.append([policy.shown.value, policy.intensity])
    return out


@pytest.fixture(scope="module")
def node() -> str:
    found = shutil.which("node")
    if found is None:
        if os.environ.get("HYPERMIND_REQUIRE_NODE"):
            pytest.fail("node is required (HYPERMIND_REQUIRE_NODE) but not on PATH")
        pytest.skip("node is not installed")
    return found


@pytest.mark.parametrize("seed", range(8))
def test_python_and_javascript_present_the_same_character(node, seed):
    runs = _runs(seed)
    result = subprocess.run([node, str(TRACE)], input=json.dumps(runs), capture_output=True, text=True,
                            timeout=60, check=True)
    js = json.loads(result.stdout)
    for i, run in enumerate(runs):
        py = _python(run)
        for j, (a, b) in enumerate(zip(py, js[i])):
            assert a == b, f"settings {run['settings']} step {j}: python {a} javascript {b}"
        assert len(py) == len(js[i])
