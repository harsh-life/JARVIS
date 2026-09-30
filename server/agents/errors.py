"""Errors of the Agent Factory's deterministic layers."""

from __future__ import annotations


class AgentRegistryError(Exception):
    """A template, profile, runtime toggle or ability mapping is invalid.

    Raised at load time and fatal to startup (the `15` §6 fail-closed pattern):
    a registry that loaded "what it could" would be a registry nobody
    reviewed."""


__all__ = ["AgentRegistryError"]
