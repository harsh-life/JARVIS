"""The integration suite drives the production composition root through the
runtime harness (`tests/runtime/conftest.py`): real OIDC → device → token
onboarding, the real Security Core, the real execution tools where a case needs
them, and a scripted model standing in for the one untrusted dependency."""

from tests.runtime.conftest import h, make_harness  # noqa: F401 — shared fixtures
