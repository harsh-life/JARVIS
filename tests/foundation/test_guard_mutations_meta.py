"""Keeps `tests/tools/guard_mutations.py` honest between full runs.

The mutation check itself is too slow for every push (it re-runs suites once
per mutant), so CI runs this instead: every mutant still names text that
occurs **exactly once** in its file, and every test file it relies on exists.
A refactor that moves a guard therefore fails here, pointing at the mutant to
re-anchor — the catalogue cannot silently decay into mutants that no longer
mutate anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.tools.guard_mutations import MUTANTS, ROOT, anchor_count


def test_mutant_ids_are_unique_and_every_edit_changes_something():
    assert len({m.id for m in MUTANTS}) == len(MUTANTS)
    assert all(m.old != m.new for m in MUTANTS)


@pytest.mark.parametrize("mutant", MUTANTS, ids=[m.id for m in MUTANTS])
def test_each_mutant_still_anchors_on_exactly_one_place(mutant):
    assert anchor_count(mutant) == 1, f"{mutant.id}: re-anchor on {mutant.path}"


@pytest.mark.parametrize("mutant", MUTANTS, ids=[m.id for m in MUTANTS])
def test_each_mutants_defending_tests_exist(mutant):
    targets = [arg for arg in mutant.command if arg.startswith("tests/")]
    assert all((ROOT / Path(t)).exists() for t in targets), targets
