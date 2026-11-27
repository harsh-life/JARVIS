"""Deployment preconditions — checked once, when the server process starts.

These are not schema rules (a test harness may configure any combination);
they are the conditions under which a *deployed* server refuses to start
(register §2N, readings 5 and 6, confirmed by the owner on 2026-10-13):

* OD-PILOT-2 — a non-local primary model is a paid dependency, so the per-task
  budget and both daily cost limits must be greater than zero;
* OD-PILOT-5 — with a non-local primary the pilot is invite-only, so the
  sign-in allow-list (`security.oidc.allowed_emails` / `allowed_domains`)
  must not be empty;
* remote exposure (2026-11-27 hardening) — whatever the model, a server that
  is reachable beyond this machine (`server.host` is not loopback, or
  `server.base_url` names a host that is not loopback or a reserved,
  non-routable name) must have an explicit admission policy: a non-empty
  allow-list, or `security.oidc.allow_open_registration: true`. With both
  lists empty the OIDC flow admits *any* verified Google account — fine for a
  laptop, never an accidental default for a public hostname.

`server/composition/main.py` calls `check_deployment` before building the
application; a failure is a `ConfigError`, the same fail-closed path as any
other invalid configuration.
"""

from __future__ import annotations

import ipaddress
import logging
from urllib.parse import urlsplit

from server.config.errors import ConfigError
from server.config.schema import LOCAL_MODEL_PROVIDERS, AppConfig

# RFC 2606 / RFC 6761 names that never route on the internet. A `base_url`
# under one of them cannot be a public deployment (test fixtures use them).
_NON_ROUTABLE_SUFFIXES = (".localhost", ".test", ".invalid", ".example")

logger = logging.getLogger("hypermind.config.preconditions")


def _is_local_name(host: str | None) -> bool:
    if not host:
        return True
    name = host.strip("[]").lower().rstrip(".")
    if name == "localhost" or name.endswith(_NON_ROUTABLE_SUFFIXES) or name in {"test", "invalid", "example"}:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def remotely_exposed(config: AppConfig) -> bool:
    """Whether this server can be reached from beyond this machine: it binds a
    non-loopback interface, or it is published under a routable host name
    (behind Caddy or a tunnel, the pilot's shape). LAN addresses count."""

    return not (_is_local_name(config.server.host) and _is_local_name(urlsplit(config.server.base_url).hostname))


def deployment_problems(config: AppConfig) -> list[str]:
    """Every precondition `config` fails, as operator-facing sentences (no values)."""

    problems: list[str] = []
    oidc = config.security.oidc
    if remotely_exposed(config) and not oidc.restricted and not oidc.allow_open_registration:
        problems.append(
            "server.host / server.base_url make this server reachable remotely, so it needs an explicit "
            "admission policy: set security.oidc.allowed_emails or allowed_domains, or (deliberately) "
            "security.oidc.allow_open_registration: true — empty allow-lists would admit any verified "
            "Google account"
        )
    if config.agent.provider in LOCAL_MODEL_PROVIDERS:
        return problems
    problems += [
        f"{name} must be greater than zero when agent.provider is not local (OD-PILOT-2)"
        for name, value in (
            ("agent.bounds.per_task_budget", config.agent.bounds.per_task_budget),
            ("security.budgets.per_user_daily_cost_limit", config.security.budgets.per_user_daily_cost_limit),
            ("security.budgets.global_daily_cost_limit", config.security.budgets.global_daily_cost_limit),
        )
        if value <= 0
    ]
    if not oidc.restricted:
        # OD-PILOT-5 has no open-registration escape: a paid primary is invite-only.
        problems.append(
            "security.oidc.allowed_emails or security.oidc.allowed_domains must be set when "
            "agent.provider is not local (OD-PILOT-5: the pilot is invite-only)"
        )
    return problems


def check_deployment(config: AppConfig) -> AppConfig:
    problems = deployment_problems(config)
    if problems:
        raise ConfigError("the server will not start: " + "; ".join(problems))
    oidc = config.security.oidc
    if not oidc.restricted:
        if remotely_exposed(config):
            logger.warning("OPEN REGISTRATION on a remotely reachable server: any verified Google account can "
                           "sign in (security.oidc.allow_open_registration: true)")
        else:
            logger.info("sign-in is open to any verified Google account (local development: no allow-list)")
    return config
