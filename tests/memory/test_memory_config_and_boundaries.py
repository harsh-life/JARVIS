"""Configuration (15), provider replaceability (docs/21 §1), and module boundaries
(16) for the memory build.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import textwrap
import tomllib
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from server.config.errors import ConfigError
from server.config.loader import load_config
from server.config.schema import MemoryConfig, VaultConfig
from server.gateway.app import API_V1_PREFIX
from server.memory.provider import MemoryCandidate
from shared.schemas.enums import Visibility
from shared.schemas.memory import Mem0Fact
from tests.memory.conftest import EMBEDDER_CACHE, require_memory_stack
from tests.support import make_test_config

REPO = Path(__file__).parents[2]
MEM = f"{API_V1_PREFIX}/memory"


# ── configuration (15 §2/§6) ───────────────────────────────────────────────


def test_defaults_are_safe_and_self_hosted():
    config = make_test_config()
    memory, vault = config.memory, config.vault
    assert memory.enabled is False and memory.auto_extract is False and memory.writes_enabled is True
    assert memory.provider == "mem0" and memory.mem0.mode == "jarvis_extraction"
    assert memory.mem0.embedder == vault.embedder == "bge-small-en-v1.5"
    assert memory.mem0.collection == "hypermind_memories" and vault.collection == "hypermind_vault"
    assert vault.enabled is False and vault.git_backed is True
    # No field anywhere in the memory/vault sections can hold a URL or a key.
    dumped = str(memory.model_dump()) + str(vault.model_dump())
    assert "http" not in dumped and "key" not in dumped.lower().replace("keys", "")


@pytest.mark.parametrize("payload", [
    {"provider": "openai"}, {"provider": "mem0-cloud"}, {"mem0": {"mode": "metering_proxy"}},
    {"mem0": {"mode": "mem0_inference"}}, {"mem0": {"api_key": "x"}}, {"mem0": {"llm": {"provider": "openai"}}},
    {"unknown": True},
])
def test_unsupported_or_cloud_memory_settings_are_load_time_failures(payload):
    with pytest.raises(ValidationError):
        MemoryConfig.model_validate(payload)


@pytest.mark.parametrize("payload", [{"git_backed": False}, {"write_api": True}, {"hydration_top_k": 50}])
def test_unsupported_vault_settings_are_load_time_failures(payload):
    with pytest.raises(ValidationError):
        VaultConfig.model_validate(payload)


def test_the_example_config_loads_with_memory_and_vault_off():
    config = load_config(REPO / "config.example.yaml")
    assert config.memory.enabled is False and config.vault.enabled is False
    text = (REPO / "config.example.yaml").read_text()
    assert "sk-" not in text and "api_key:" not in text


def test_an_enabled_but_unprovisioned_memory_stops_the_server_without_downloading(tmp_path, egress):
    from server.composition import build_application

    require_memory_stack()
    config = make_test_config(memory={"enabled": True, "mem0": {
        "path": str(tmp_path / "m"), "embedder_cache": str(tmp_path / "no_model")}})
    egress.active = True
    with pytest.raises(ConfigError, match="python -m server.memory provision"):
        build_application(config)
    egress.active = False
    assert egress.network() == []


def test_an_enabled_but_unprovisioned_vault_stops_the_server(tmp_path):
    from server.composition import build_application

    require_memory_stack()
    config = make_test_config(vault={"enabled": True, "index_path": str(tmp_path / "v"),
                                     "embedder_cache": str(tmp_path / "no_model")})
    with pytest.raises(ConfigError, match="vault.enabled"):
        build_application(config)


async def test_an_enabled_memory_is_wired_end_to_end_from_config_alone(tmp_path, monkeypatch):
    """No injection: `build_application(config)` opens the Mem0 store, registers
    the `mem0fact` loader with the engine, and serves the memory API."""

    import httpx

    from server.composition import build_application
    from server.gateway.security import build_security_core
    from server.secrets.kek import resolve_kek
    from server.storage import SQLAlchemyStorageBackend
    from tests.runtime.conftest import Harness, ScriptedModel
    from tests.support import TEST_KEK_ENV_VAR, LocalOIDCProvider, make_test_kek

    require_memory_stack()
    monkeypatch.setenv(TEST_KEK_ENV_VAR, make_test_kek())
    config = make_test_config(memory={"enabled": True, "mem0": {
        "path": str(tmp_path / "m"), "embedder_cache": str(EMBEDDER_CACHE)}})
    storage = SQLAlchemyStorageBackend(f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    await storage.init_models()
    oidc = LocalOIDCProvider()
    core = build_security_core(config, oidc_provider=oidc)
    async with storage.session() as s:
        await core.secret_store.bootstrap(s, resolve_kek(f"env:{TEST_KEK_ENV_VAR}"))
        await s.commit()
    app = build_application(config, storage=storage, security=core, extra_tools=())
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        h = Harness(client=client, oidc=oidc, core=core, storage=storage, config=config, model=ScriptedModel(),
                    models={}, reads=None, writes=None, ui=None, memory=None, app=app)
        alice = await h.user("alice")
        await h.shared_graph(alice)
        status = (await client.get(f"{MEM}/status", headers=alice.auth)).json()
        assert status["enabled"] is True and status["available"] is True
        created = await client.post(MEM, json={"fact_type": "preference", "content": "The user prefers tea"},
                                    headers=alice.auth)
        assert created.status_code == 201, created.text
        with pytest.raises(ValueError, match="already exists"):
            from shared.schemas.authorization import ResourceType

            core.resource_loader.register(ResourceType.MEM0FACT, object())
    finally:
        await client.aclose()
        await storage.dispose()


def test_the_provision_command_is_idempotent_and_offline_once_provisioned(tmp_path, egress, capsys):
    from server.memory.__main__ import main

    require_memory_stack()
    import yaml

    config = make_test_config(memory={"mem0": {"embedder_cache": str(EMBEDDER_CACHE), "path": str(tmp_path / "m")}},
                              vault={"embedder_cache": str(EMBEDDER_CACHE), "index_path": str(tmp_path / "v")})
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    egress.active = True
    assert main(["provision", "--config", str(path)]) == 0
    egress.active = False
    assert egress.network() == []
    assert "provisioned bge-small-en-v1.5" in capsys.readouterr().out


# ── provider replaceability (docs/21 §1, OWNER-RATIFIED) ──────────────────


class DictMemoryProvider:
    """A second, trivial `MemoryProvider`: replacing Mem0 is an adapter plus
    config, with no change to authorization, runtime or API."""

    def __init__(self) -> None:
        self.facts: dict[uuid.UUID, Mem0Fact] = {}

    async def search(self, *, query, owner_user_id, readable_graph_ids, limit):
        return [MemoryCandidate(str(f.fact_id), f.content, f.owner_user_id, f.visibility, f.graph_id, 1.0)
                for f in self.facts.values()
                if f.owner_user_id == owner_user_id
                or (f.visibility is Visibility.GRAPH and f.graph_id in readable_graph_ids)][:limit]

    async def add(self, fact):
        stored = fact.model_copy(update={"visibility": Visibility.PRIVATE})
        self.facts[stored.fact_id] = stored
        return stored.fact_id

    async def get(self, fact_id):
        return self.facts.get(fact_id)

    async def list_facts(self, *, owner_user_id, readable_graph_ids, limit):
        found = await self.search(query="", owner_user_id=owner_user_id, readable_graph_ids=readable_graph_ids,
                                  limit=limit)
        return [self.facts[uuid.UUID(c.fact_id)] for c in found]

    async def update_content(self, fact_id, content):
        self.facts[fact_id] = self.facts[fact_id].model_copy(update={"content": content})

    async def set_visibility(self, fact_id, visibility):
        self.facts[fact_id] = self.facts[fact_id].model_copy(update={"visibility": visibility})

    async def delete(self, fact_id):
        return self.facts.pop(fact_id, None) is not None

    async def delete_all_for_user(self, user_id):
        doomed = [k for k, f in self.facts.items() if f.owner_user_id == user_id]
        for k in doomed:
            del self.facts[k]
        return len(doomed)

    async def delete_graph_shared(self, graph_id):
        doomed = [k for k, f in self.facts.items() if f.graph_id == graph_id and f.visibility is Visibility.GRAPH]
        for k in doomed:
            del self.facts[k]
        return len(doomed)

    async def health(self):
        return True


async def test_a_different_provider_plugs_in_with_the_same_authorization(make_harness):
    provider = DictMemoryProvider()
    h = await make_harness(memory_provider=provider)
    alice, bob = await h.user("alice"), await h.user("bob")
    await h.shared_graph(alice, bob)
    fact_id = (await h.client.post(MEM, json={"fact_type": "preference", "content": "The user prefers tea"},
                                   headers=alice.auth)).json()["fact_id"]
    # Same engine, same rules: Bob cannot see or delete Alice's private fact.
    assert (await h.client.get(MEM, headers=bob.auth)).json()["items"] == []
    assert (await h.client.delete(f"{MEM}/{fact_id}", headers=bob.auth)).status_code == 404
    assert (await h.client.get(MEM, headers=alice.auth)).json()["items"][0]["content"] == "The user prefers tea"


def test_nothing_imports_mem0_unless_memory_is_enabled():
    code = textwrap.dedent("""
        import sys
        from server.composition import build_application
        from server.memory import AuthorizedContextHydrator, MemoryWriteGate
        from server.gateway.app import create_app
        assert "mem0" not in sys.modules, "mem0 imported without memory enabled"
        assert "chromadb" not in sys.modules
    """)
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# ── module boundaries (16 §6, REPO-T7) ─────────────────────────────────────


MEMORY_CONTRACTS = (
    "Memory/vault never resolve secrets (12 §6, GRAPH-009)",
    "Memory and vault are separate stores (VAULT-003, MEM-T6/T7)",
    "Memory/vault reach no control, device, scheduler, voice or judge code (docs/21)",
    "Only the Mem0 adapter imports mem0 (docs/21 §2)",
    "Only the gateway reaches superuser authority (12 §4, SUPER-001)",
)


def _lint(config: Path, cwd: Path) -> subprocess.CompletedProcess:
    script = Path(sys.executable).parent / "lint-imports"
    return subprocess.run([str(script), "--config", str(config)], cwd=cwd, capture_output=True, text=True)


def test_every_memory_contract_is_declared_and_kept():
    result = _lint(REPO / "pyproject.toml", REPO)
    assert result.returncode == 0, result.stdout
    for name in MEMORY_CONTRACTS:
        assert re.search(re.escape(name) + r"\s*KEPT", result.stdout), name
    contracts = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["importlinter"]["contracts"]
    superuser = next(c for c in contracts if c["name"] == MEMORY_CONTRACTS[4])
    assert {"server.memory", "server.vault"} <= set(superuser["source_modules"])


def _fixture_project(tmp_path: Path, violation: tuple[str, str]) -> Path:
    """A throwaway tree with the real package names, one deliberate violation,
    and the repository's *own* memory contracts copied verbatim."""

    root = tmp_path / "fixture"
    packages = ["server", "server/memory", "server/vault", "server/secrets", "server/dashboard", "server/scheduler",
                "server/voice", "server/evaluation", "server/intelligence", "server/execution", "server/agent",
                "server/tools", "server/modeltools", "server/gateway", "server/composition", "server/security",
                "server/auth", "server/graph", "server/capabilities", "server/models", "server/fs", "server/net",
                "server/config", "server/storage", "shared", "mem0"]
    for pkg in packages:
        (root / pkg).mkdir(parents=True, exist_ok=True)
        (root / pkg / "__init__.py").write_text("")
    (root / "server/security/superuser.py").write_text("")
    (root / "server/gateway/superuser_auth.py").write_text("")
    (root / "server/gateway/control_port.py").write_text("")
    (root / "server/gateway/routers").mkdir()
    (root / "server/gateway/routers/__init__.py").write_text("")
    (root / "server/gateway/routers/control.py").write_text("")
    for name in ("supervisor", "latch", "break_glass"):
        (root / f"server/composition/{name}.py").write_text("")
    (root / "server/memory/mem0_provider.py").write_text("import mem0\n")
    source, target = violation
    (root / source).write_text(f"import {target}\n")

    contracts = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["importlinter"]["contracts"]
    chosen = [c for c in contracts if c["name"] in MEMORY_CONTRACTS]
    lines = ['[tool.importlinter]', 'root_packages = ["server", "shared", "mem0"]', ""]
    for contract in chosen:
        lines.append("[[tool.importlinter.contracts]]")
        for key, value in contract.items():
            lines.append(f"{key} = {value!r}".replace("'", '"').replace("True", "true").replace("False", "false"))
        lines.append("")
    (root / "fixture.toml").write_text("\n".join(lines))
    return root


@pytest.mark.parametrize("violation", [
    ("server/memory/leak.py", "server.secrets"),
    ("server/vault/leak.py", "server.secrets"),
    ("server/memory/leak.py", "server.vault"),
    ("server/vault/leak.py", "server.memory"),
    ("server/memory/leak.py", "server.dashboard"),
    ("server/vault/leak.py", "server.scheduler"),
    ("server/memory/leak.py", "server.execution"),
    ("server/memory/leak.py", "server.security.superuser"),
    ("server/gateway/leak.py", "mem0"),
    ("server/vault/leak.py", "mem0"),
])
def test_each_memory_contract_detects_a_deliberate_violation(tmp_path, violation):
    root = _fixture_project(tmp_path, violation)
    clean = _fixture_project(tmp_path / "clean", ("server/memory/ok.py", "server.graph"))
    assert _lint(clean / "fixture.toml", clean).returncode == 0
    result = _lint(root / "fixture.toml", root)
    assert result.returncode != 0 and "BROKEN" in result.stdout, result.stdout


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_third_party_stores_and_process_primitives_are_confined_to_their_one_module():
    allowed = {
        "mem0": {"server/memory/mem0_provider.py"},
        "chromadb": {"server/memory/mem0_provider.py", "server/vault/index.py"},
        "fastembed": {"server/models/embedding.py"},
        "dulwich": {"server/vault/ingest.py"},
    }
    for path in sorted((REPO / "server").rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        top = {name.split(".")[0] for name in _imports(path)}
        for package, homes in allowed.items():
            if homes is not None and package in top:
                assert rel in homes, f"{rel} imports {package}"
        if rel.startswith(("server/memory/", "server/vault/")):
            assert not top & {"socket", "subprocess", "shutil", "urllib", "http", "httpx", "requests"}, rel
            assert not any(name.startswith("server.secrets") for name in _imports(path)), rel


def test_no_secret_or_endpoint_literal_in_memory_code():
    for folder in ("server/memory", "server/vault"):
        for path in (REPO / folder).rglob("*.py"):
            text = path.read_text()
            assert "api.openai.com" not in text and "api.mem0.ai" not in text, path
            assert not re.search(r"sk-[A-Za-z0-9]{20,}", text), path
