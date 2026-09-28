"""Config loading/validation tests (15_CONFIGURATION_SELF_HOSTING.md §8:
CFG-T3/T4/T5/T6; CFG-T1 partially — see note below)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from server.config import ConfigError, load_config

MINIMAL_VALID_YAML = """
security:
  oidc:
    client_id: "test-client-id"
    issuer: "https://accounts.google.com"
secrets:
  store: "encrypted_local"
  kek_source: "env:TEST_KEK"
"""


def test_example_config_loads_and_uses_no_owner_secrets(tmp_path: Path) -> None:
    """CFG-T1 (partial, unit-level): the tracked config.example.yaml
    contains only placeholder values / secret_ref-style references — never
    a real credential — and loads cleanly. The full CFG-T1 self-host claim
    (a fresh clone runs end-to-end on the cloner's own credentials) also
    depends on auth/model/tool wiring this branch does not implement; this
    test covers the config-loading half foundation owns.
    """

    example = Path(__file__).parents[2] / "config.example.yaml"
    cfg = load_config(example)
    assert cfg.intelligence.enabled is False
    assert cfg.security.oidc.client_id.startswith("REPLACE_WITH_YOUR_OWN")


def test_missing_config_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "does-not-exist.yaml")


def test_malformed_yaml_fails_closed(tmp_path: Path) -> None:
    bad = tmp_path / "config.yaml"
    bad.write_text("server: [this is not: valid: yaml")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_minimal_valid_config_loads(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(MINIMAL_VALID_YAML)
    cfg = load_config(cfg_file)
    assert cfg.intelligence.enabled is False  # INTEL-003 default
    assert cfg.memory.mem0.collection != cfg.vault.collection


@pytest.mark.parametrize("url", [
    "postgresql+asyncpg://jarvis@127.0.0.1:5432/jarvis",
    "postgresql+asyncpg://jarvis@db.internal/jarvis?ssl=require",
    "sqlite+aiosqlite:///./data/hypermind.db",
])
def test_the_supported_database_urls_load(tmp_path: Path, url: str) -> None:
    """H-1: PostgreSQL (the pilot's runtime store) and SQLite (development)."""

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(MINIMAL_VALID_YAML + f'database_url: "{url}"\n')
    assert load_config(cfg_file).database_url == url


@pytest.mark.parametrize("url", [
    "postgresql+asyncpg://jarvis:TESTONLYpw@127.0.0.1:5432/jarvis",  # TEST-ONLY placeholder
    "postgresql+asyncpg://jarvis@127.0.0.1/jarvis?password=TESTONLYpw",  # TEST-ONLY placeholder
    "postgresql://jarvis@127.0.0.1/jarvis",   # a sync driver the async engine cannot use
    "mysql+aiomysql://jarvis@127.0.0.1/jarvis",
    "not a url",
])
def test_an_unsupported_or_credential_bearing_database_url_fails_closed(tmp_path: Path, url: str) -> None:
    """SECRET-004 for the store: a password never sits in configuration (it
    comes from PGPASSWORD or ~/.pgpass), and an unsupported driver fails at load
    rather than at the first request. The refusal never echoes the value."""

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(MINIMAL_VALID_YAML + f'database_url: "{url}"\n')
    with pytest.raises(ConfigError) as refused:
        load_config(cfg_file)
    assert "TESTONLYpw" not in str(refused.value)


def test_literal_secret_rejected_at_load(tmp_path: Path) -> None:
    """CFG-T6: a config bearing a literal secret fails validation at load."""

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        MINIMAL_VALID_YAML
        + textwrap.dedent(
            """
            agent:
              provider: "openai"
              model: "gpt-4"
              secret_ref: "NOT-A-SECRET-REF-literal-value-for-CFG-T6-do-not-use"
            """
        )
    )
    with pytest.raises(ConfigError) as refused:
        load_config(cfg_file)
    # The refusal names the field, never the value (it is logged at startup).
    assert "agent.secret_ref" in str(refused.value)
    assert "literal-value-for-CFG-T6" not in str(refused.value)
    assert refused.value.__cause__ is None and refused.value.__suppress_context__


def test_mem0_and_vault_same_collection_rejected(tmp_path: Path) -> None:
    """CFG-T4: mem0 and vault collections must be distinct; a same-value
    config fails to start (VAULT-003)."""

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        MINIMAL_VALID_YAML
        + textwrap.dedent(
            """
            memory:
              mem0:
                collection: "same_name"
            vault:
              collection: "same_name"
            """
        )
    )
    with pytest.raises(ConfigError):
        load_config(cfg_file)


def test_intelligence_defaults_disabled(tmp_path: Path) -> None:
    """INTEL-003 / CFG-T5 (config half): intelligence.enabled defaults
    false when omitted entirely."""

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(MINIMAL_VALID_YAML)
    cfg = load_config(cfg_file)
    assert cfg.intelligence.enabled is False


def test_no_secret_literal_in_repo_config_templates() -> None:
    """CFG-T3 (template half): config.example.yaml never contains a literal
    secret — every credential-shaped field is a secret_ref or absent."""

    example = Path(__file__).parents[2] / "config.example.yaml"
    text = example.read_text()
    # Every non-comment, non-empty line assigning *_ref/kek_source must use
    # the env:/secretstore: convention if present at all.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if "secret_ref" in stripped or "kek_source" in stripped or "config_ref" in stripped:
            assert "env:" in stripped or "secretstore:" in stripped or "#" in stripped
