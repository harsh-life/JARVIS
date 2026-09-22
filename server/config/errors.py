"""Config loading is fail-closed (15_CONFIGURATION_SELF_HOSTING.md §6):
"A malformed/secret-bearing config fails to start (fail-closed), with an
explicit message — never a silent partial start."
"""

from __future__ import annotations


class ConfigError(Exception):
    """Raised when configuration is missing, malformed, or contains a
    literal secret. The process must not start on this error."""
