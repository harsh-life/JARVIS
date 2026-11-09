"""What the Workbench reports must match what the harness implements, and the
harness must stay isolated from production state."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from experiments.contracts import BODY_MODELS
from experiments.orchestration.backends import IMPLEMENTED
from experiments.tasks import load_suite
from shared.schemas.intelligence import HARNESS_ADAPTERS, OUTPUT_SCHEMAS

REPO = Path(__file__).resolve().parents[2]


def test_the_server_reports_exactly_the_adapters_the_harness_implements():
    assert HARNESS_ADAPTERS == IMPLEMENTED


def test_every_output_schema_the_catalogue_may_name_has_a_body_model():
    assert OUTPUT_SCHEMAS == set(BODY_MODELS)


def _loaded(code: str) -> set[str]:
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True)
    return set(out.stdout.split())


def test_the_harness_loads_only_config_and_model_adapters_from_server():
    loaded = _loaded(
        "import sys, experiments.__main__, experiments.runner\n"
        "print(' '.join(sorted({'.'.join(m.split('.')[:2]) for m in sys.modules if m.startswith('server.')})))"
    )
    assert loaded <= {"server.config", "server.models"}, loaded


def test_production_never_loads_the_harness():
    # The whole production composition root (main.py itself only adds loading
    # config.yaml at import time).
    loaded = _loaded("import sys, server.composition, server.composition.facade, server.gateway.app\n"
                     "print(' '.join(m for m in sys.modules if m.split('.')[0] == 'experiments'))")
    assert loaded == set()


def test_the_suite_is_24_to_30_synthetic_tasks_covering_every_category():
    suite = load_suite()
    assert 24 <= len(suite.tasks) <= 30
    assert {t.category for t in suite.tasks} == {"factual", "planning", "contradiction", "research", "ambiguous",
                                                 "technical", "injection", "unauthorized_action"}
    assert sum(1 for t in suite.tasks if t.decomposition) >= 10
