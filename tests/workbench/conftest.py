"""The Workbench suite drives the production composition root through the
runtime harness (`tests/runtime/conftest.py`): real OIDC → device → token
onboarding and the real Security Core."""

from tests.runtime.conftest import h, make_harness  # noqa: F401 — shared fixtures
