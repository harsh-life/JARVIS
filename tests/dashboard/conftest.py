"""The console suites reuse the runtime harness: the production composition
root, the real Security Core, superuser authentication and ledgers."""

from tests.runtime.conftest import make_harness  # noqa: F401  (fixture re-export)
