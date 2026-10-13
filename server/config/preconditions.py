"""Deployment preconditions — checked once, when the server process starts.

These are not schema rules (a test harness may configure any combination);
they are the conditions under which a *deployed* server refuses to start
(register §2N, readings 5 and 6, confirmed by the owner on 2026-10-13):

* OD-PILOT-2 — a non-local primary model is a paid dependency, so the per-task
  budget and both daily cost limits must be greater than zero;
* OD-PILOT-5 — with a non-local primary the pilot is invite-only, so the
  sign-in allow-list (`security.oidc.allowed_emails` / `allowed_domains`)
  must not be empty.

`server/composition/main.py` calls `check_deployment` before building the
application; a failure is a `ConfigError`, the same fail-closed path as any
other invalid configuration.
"""

from __future__ import annotations

from server.config.errors import ConfigError
from server.config.schema import LOCAL_MODEL_PROVIDERS, AppConfig


def deployment_problems(config: AppConfig) -> list[str]:
    """Every precondition `config` fails, as operator-facing sentences (no values)."""

    if config.agent.provider in LOCAL_MODEL_PROVIDERS:
        return []
    problems = [
        f"{name} must be greater than zero when agent.provider is not local (OD-PILOT-2)"
        for name, value in (
            ("agent.bounds.per_task_budget", config.agent.bounds.per_task_budget),
            ("security.budgets.per_user_daily_cost_limit", config.security.budgets.per_user_daily_cost_limit),
            ("security.budgets.global_daily_cost_limit", config.security.budgets.global_daily_cost_limit),
        )
        if value <= 0
    ]
    if not config.security.oidc.restricted:
        problems.append(
            "security.oidc.allowed_emails or security.oidc.allowed_domains must be set when "
            "agent.provider is not local (OD-PILOT-5: the pilot is invite-only)"
        )
    return problems


def check_deployment(config: AppConfig) -> AppConfig:
    problems = deployment_problems(config)
    if problems:
        raise ConfigError("the server will not start: " + "; ".join(problems))
    return config
