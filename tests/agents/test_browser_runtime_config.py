"""The Browser Use container's own configuration, read from its source.

Regression for the `containers` failure of 2026-10-13 (PR #45, run 148,
attempts 1 and 2): `test_a_real_browser_run_goes_only_through_jarvis` ended
`runtime_failed` because Browser Use's default 30 s budget for the whole
browser start-up equalled the 30 s inner timeout of one start-up handler, so a
single stalled `Page.enable` under gVisor (about one start in ten) failed the
run before Browser Use's own recovery could run. Reproduced locally with
rootless Podman + gVisor `runsc` 20261005.0 and Chromium
155.0.8059.39-1~deb12u1: with the budget raised, the stalled start took 35 s
and the run completed (12 of 12 passed).

Read statically, so it runs everywhere (no container stack needed); the live
run is `test_browser_live.py`.
"""

from __future__ import annotations

import ast
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parents[2] / "runtime" / "browser_use" / "entrypoint.py"

# Browser Use 0.13.10's inner per-handler timeout during start-up, and a
# normal start under gVisor (measured 4.6–5.8 s).
INNER_HANDLER_TIMEOUT_S = 30.0
NORMAL_START_S = 6.0


def _module() -> ast.Module:
    return ast.parse(ENTRYPOINT.read_text(encoding="utf-8"), str(ENTRYPOINT))


def _timeouts() -> dict[str, str]:
    for node in _module().body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "BROWSER_USE_TIMEOUTS" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("BROWSER_USE_TIMEOUTS is not defined in the entrypoint")


def test_the_start_budget_outlasts_one_stalled_start_handler():
    budget = float(_timeouts()["TIMEOUT_BrowserStartEvent"])
    assert budget >= 2 * INNER_HANDLER_TIMEOUT_S + NORMAL_START_S, budget


def test_only_the_start_budget_is_changed():
    # Every other Browser Use limit keeps its default; nothing here loosens
    # what the runtime may do, only how long its own start-up may take.
    assert set(_timeouts()) == {"TIMEOUT_BrowserStartEvent"}


def test_the_budget_is_applied_to_the_process_environment_before_browser_use_loads():
    source = ENTRYPOINT.read_text(encoding="utf-8")
    applied = source.index("**BROWSER_USE_TIMEOUTS")
    assert applied < source.index("from browser_use import Agent")
    assert "os.environ.update({" in source[: applied]
