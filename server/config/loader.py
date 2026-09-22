"""Config loader (15_CONFIGURATION_SELF_HOSTING.md §3/§6).

Format choice [IMPL, OD-CFG-1]: YAML. Rationale (documented per this
branch's instruction to record [IMPL] choices): YAML is human-editable,
supports nested structure natively (the config surface in 15 §2 is
inherently nested), and is the most common self-hosting config format for
comparable projects — the simplest choice satisfying the locked shape
constraint. TOML or a fully env-layered scheme would work equally well and
are not precluded later; nothing about this loader locks the *runtime*
behavior described in 15, only the file it reads.

Fail-closed [LOCKED]: any missing file, malformed YAML, schema violation
(including a literal-looking secret or a mem0==vault collision) raises
ConfigError. There is no partial/degraded start.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import ValidationError

from server.config.errors import ConfigError
from server.config.schema import AppConfig

DEFAULT_CONFIG_ENV_VAR = "HYPERMIND_CONFIG_PATH"
DEFAULT_CONFIG_PATH = "config.yaml"


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load and validate the application config.

    Resolution order for `path` when not given explicitly:
    1. `$HYPERMIND_CONFIG_PATH`
    2. `./config.yaml` (repo-root default; git-ignored — see .gitignore and
       config.example.yaml)
    """

    resolved = Path(path) if path is not None else Path(
        os.environ.get(DEFAULT_CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH)
    )

    if not resolved.exists():
        raise ConfigError(
            f"config file not found: {resolved} (copy config.example.yaml "
            f"to {DEFAULT_CONFIG_PATH}, or set {DEFAULT_CONFIG_ENV_VAR})"
        )

    try:
        raw_text = resolved.read_text()
    except OSError as exc:
        raise ConfigError(f"could not read config file {resolved}: {exc}") from exc

    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file {resolved} is not valid YAML: {exc}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"config file {resolved} must be a YAML mapping at the top level")

    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"config file {resolved} failed validation:\n{exc}") from exc
