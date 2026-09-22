"""server/config/schema.py's ExecutionConfig — additive to 15 §2.

Defaults must be safe-by-default (default-deny network, empty process
allowlist, mediated/dev-fallback containment honestly labelled) so that a
fresh clone with no `execution:` section in its config still starts in the
most restrictive posture, not the most permissive one.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from server.config.schema import (
    ExecutionConfig,
    FilesystemSandboxConfig,
    NetworkEgressConfig,
    ProcessExecutionConfig,
)


def test_defaults_are_the_dev_fallback_mode_not_a_false_stronger_claim():
    fs = FilesystemSandboxConfig()
    net = NetworkEgressConfig()
    assert fs.containment_mode == "mediated"
    assert net.enforcement_mode == "mediated_proxy"


def test_process_allowlist_defaults_to_empty_deny_all():
    process = ProcessExecutionConfig()
    assert process.allowed_executables == []


def test_containment_mode_rejects_unknown_values():
    with pytest.raises(ValidationError):
        FilesystemSandboxConfig(containment_mode="trust_me")


def test_enforcement_mode_rejects_unknown_values():
    with pytest.raises(ValidationError):
        NetworkEgressConfig(enforcement_mode="none")


def test_execution_config_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        ExecutionConfig.model_validate({"unexpected_section": {}})


def test_execution_config_defaults_construct_cleanly():
    config = ExecutionConfig()
    assert config.filesystem.max_file_bytes > 0
    assert config.network.max_response_bytes > 0
    assert config.process.max_timeout_seconds >= config.process.default_timeout_seconds
