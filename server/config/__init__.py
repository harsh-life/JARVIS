"""Configuration foundation (15_CONFIGURATION_SELF_HOSTING.md).

Implements the config *shape*, validation, and loader described in
00_CANONICAL_PRD.md §33 and 15_CONFIGURATION_SELF_HOSTING.md. Foundation
does not implement the systems this config describes (ModelProvider, tools,
memory/vault backends, IntelligenceProvider, voice, SecretStore) — only the
config surface those later branches will read.
"""

from server.config.errors import ConfigError
from server.config.loader import load_config
from server.config.schema import AppConfig

__all__ = ["AppConfig", "ConfigError", "load_config"]
