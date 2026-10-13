"""The pilot deployment artifacts (docs/PILOT_EXECUTION_PLAN.md F1–F6,
docs/DEPLOYMENT.md), checked statically: the pilot configuration loads and
meets the startup preconditions, the unit and its start script keep secrets
out of files and bind loopback, the bootstrap never replaces a key, and the
two proxy configurations expose exactly the server's public surface.

What these cannot show — a real host, certificate, tunnel or phone — is the
M1 exit check in docs/DEPLOYMENT.md §5.3.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from server.composition.agents import registries_from_config
from server.config import load_config
from server.config.preconditions import check_deployment, deployment_problems
from server.config.schema import LOCAL_MODEL_PROVIDERS
from server.gateway.app import API_V1_PREFIX, create_app
from server.gateway.routers.auth import public_router
from server.gateway.security import build_security_core
from tests.support import make_test_config

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
PILOT = ROOT / "config.pilot.example.yaml"


# ── F1: the pilot configuration ────────────────────────────────────────────

@pytest.fixture(scope="module")
def pilot():
    return load_config(PILOT)


def test_the_pilot_config_loads_and_meets_the_startup_preconditions(pilot):
    assert pilot.agent.provider not in LOCAL_MODEL_PROVIDERS        # P5: a remote primary
    assert deployment_problems(pilot) == []
    assert check_deployment(pilot) is pilot


def test_the_pilot_config_carries_the_pilot_decisions(pilot):
    assert pilot.agent.pricing is not None                           # P5: priced
    assert pilot.security.oidc.restricted                            # P19 / OD-PILOT-5
    assert pilot.models_as_tools == []                               # P1
    assert pilot.memory.enabled and pilot.memory.auto_extract        # P9
    assert pilot.vault.enabled and pilot.vault.git_backed            # P12
    assert pilot.agents.enabled and not pilot.agents.unattended_enabled   # P13 / P14
    assert {k for k, v in pilot.agents.runtimes.items() if v.enabled} == {"native"}   # P13
    assert "browser_monitor" not in pilot.agents.enabled_templates
    assert pilot.evaluation.enabled and pilot.evaluation.evaluator == "rules"         # D7
    assert pilot.evaluation.provider is None
    assert pilot.voice.providers == [] and pilot.android.push.provider == "none"      # P22 / H-2
    assert pilot.execution.process.break_glass.enabled is False
    for apps in (pilot.android.app_classification.non_sensitive,
                 pilot.android.app_classification.sensitive,
                 pilot.android.app_classification.payment):
        assert apps == []                                            # P16: ships empty


def test_the_pilot_config_is_served_on_loopback_behind_one_https_host(pilot):
    assert pilot.server.host == "127.0.0.1"
    assert pilot.server.base_url.startswith("https://")
    assert pilot.android.app_links.base_url == pilot.server.base_url   # F6: one host


def test_the_pilot_store_is_postgres_with_no_password(pilot):
    assert pilot.database_url.startswith("postgresql+asyncpg://")
    assert ":" not in pilot.database_url.split("://", 1)[1].split("@", 1)[0]


def test_the_pilot_config_holds_references_not_secrets():
    raw = yaml.safe_load(PILOT.read_text())
    assert raw["agent"]["secret_ref"].startswith("env:")
    assert raw["secrets"]["kek_source"] == "env:HYPERMIND_KEK"


def test_the_pilot_agents_registries_build(pilot):
    registries = registries_from_config(pilot)
    assert "general-agentic" in registries.model_profiles


def test_the_development_example_stays_local():
    # P5 amends OD-MT-2 by configuration only: the default example is unchanged.
    assert load_config(ROOT / "config.example.yaml").agent.provider in LOCAL_MODEL_PROVIDERS


# ── F4: the unit and its start script ──────────────────────────────────────

def _directives(text: str) -> list[tuple[str, str]]:
    return [tuple(line.split("=", 1)) for line in text.splitlines()
            if line and not line.startswith(("#", "[")) and "=" in line]


def test_the_unit_runs_unprivileged_owner_only_and_restarts():
    directives = _directives((DEPLOY / "jarvis.service").read_text())
    values = dict(directives)
    assert values["User"] == values["Group"] == "jarvis"
    assert values["UMask"] == "0077"
    assert values["Restart"] == "on-failure"
    assert values["NoNewPrivileges"] == "yes"
    assert values["ProtectSystem"] == "strict" and values["ReadWritePaths"] == "/var/lib/jarvis"
    assert values["CapabilityBoundingSet"] == ""
    assert values["ExecStart"] == "/opt/jarvis/deploy/jarvis-start.sh"


def test_the_unit_holds_no_secret_and_takes_the_kek_as_a_credential():
    text = (DEPLOY / "jarvis.service").read_text()
    directives = _directives(text)
    credentials = [v for k, v in directives if k == "LoadCredential"]
    assert "kek:/etc/jarvis/credentials/kek" in credentials
    assert not any(k == "EnvironmentFile" for k, _ in directives)
    for key, value in directives:
        if key == "Environment":
            assert not re.search(r"KEK|TOKEN|KEY|PASSWORD|SECRET", value.split("=", 1)[0]), value


def test_the_start_script_binds_loopback_and_logs_no_request_lines():
    text = (DEPLOY / "jarvis-start.sh").read_text()
    assert '"$CREDENTIALS_DIRECTORY/kek"' in text
    assert "--host 127.0.0.1" in text
    assert "--forwarded-allow-ips=127.0.0.1" in text
    assert "--no-access-log" in text
    assert "0.0.0.0" not in text


@pytest.mark.parametrize(("shell", "script"), [("sh", "jarvis-start.sh"), ("bash", "bootstrap.sh")])
def test_the_scripts_parse(shell, script):
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} not installed")
    subprocess.run([shell, "-n", str(DEPLOY / script)], check=True)


# ── F2: the bootstrap never replaces what exists ───────────────────────────

def test_the_bootstrap_creates_the_kek_and_config_only_when_absent():
    text = (DEPLOY / "bootstrap.sh").read_text()
    assert 'if [ ! -s "$CRED_DIR/kek" ]; then' in text
    assert 'if [ ! -f "$CONFIG" ]; then' in text
    assert 'if [ "$WITH_SUPERUSER" -eq 1 ] && [ ! -s "$CRED_DIR/superuser_token" ]; then' in text
    assert "bootstrap=True" in text                    # unlocks, never re-creates (12 §3)
    assert "downgrade" not in text and "dropdb" not in text


def test_the_bootstrap_never_puts_the_kek_on_a_command_line():
    for line in (DEPLOY / "bootstrap.sh").read_text().splitlines():
        if "as_jarvis" in line and "KEK" in line:
            assert "export HYPERMIND_KEK" in line, line


# ── F5: the proxies expose exactly the server's public surface ─────────────

def _caddy_paths() -> set[str]:
    match = re.search(r"@jarvis path (.+)", (DEPLOY / "Caddyfile").read_text())
    assert match
    return set(match.group(1).split())


def _caddy_admits(path: str, patterns: set[str]) -> bool:
    return any(path == p or (p.endswith("*") and path.startswith(p[:-1])) for p in patterns)


def _tunnel_regex() -> re.Pattern[str]:
    ingress = yaml.safe_load((DEPLOY / "cloudflared.example.yml").read_text())["ingress"]
    assert ingress[-1] == {"service": "http_status:404"}
    return re.compile(ingress[0]["path"])


def _served_paths() -> set[str]:
    # Everything in the schema, plus the public router's unlisted routes (the
    # App Link return and assetlinks.json). The device WebSocket is under
    # API_V1_PREFIX, like every other v1 route.
    config = make_test_config()
    app = create_app(config=config, security=build_security_core(config))
    paths = {p for p in app.openapi()["paths"]}
    paths |= {route.path for route in public_router.routes}
    paths.add(f"{API_V1_PREFIX}/devices/channel")
    return paths


def test_every_route_the_server_serves_is_reachable_through_either_proxy():
    caddy, tunnel = _caddy_paths(), _tunnel_regex()
    for path in _served_paths():
        assert _caddy_admits(path, caddy), path
        assert tunnel.search(path), path


@pytest.mark.parametrize("path", ["/", "/admin", "/metrics", "/uix", "/app/login/extra", "/.well-known/other"])
def test_nothing_else_is_forwarded(path):
    assert not _caddy_admits(path, _caddy_paths())
    assert not _tunnel_regex().search(path)


def test_caddy_throttles_the_unauthenticated_endpoints_and_strips_query_strings():
    text = (DEPLOY / "Caddyfile").read_text()
    assert "@unauthenticated path /api/v1/auth/* /api/v1/sessions/token" in text
    assert "rate_limit @unauthenticated" in text
    assert "reverse_proxy 127.0.0.1:8000" in text
    assert "Strict-Transport-Security" in text
    assert re.search(r"max_size \d+MB", text)
    assert 'request>uri regexp "\\?.*$"' in text
