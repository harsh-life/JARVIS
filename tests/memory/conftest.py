"""Fixtures for the memory build (11, docs/21): the **real** self-hosted stack.

Nothing on the security path is mocked here. `mem0_provider` is the production
adapter (`build_mem0_provider`) over a real Chroma store in `tmp_path`, with the
real offline bge-small embedder; the harness is the production composition root
from `tests/runtime/conftest.py`.

**No silent skips in CI.** The embedding model is provisioned once, by the
sanctioned command (`python -m server.memory provision`), into
`HYPERMIND_TEST_EMBEDDER_CACHE` (default `./data/models`). Locally, a missing
model skips these suites with the command to run; with
`HYPERMIND_REQUIRE_MEMORY_STACK=1` — set in CI — it is a failure instead, so the
release-blocking MEM-T1 cannot pass by not running.

**Egress guard.** `egress` records, through a CPython audit hook, every socket
connect / DNS lookup / urllib request / subprocess spawn, and every file opened
for writing outside the directories a test allows. The hook is process-wide, so
it sees Mem0 and Chroma calls made in worker threads too.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest
import pytest_asyncio

from tests.runtime.conftest import h, make_harness  # noqa: F401 — shared fixtures

EMBEDDER_CACHE = Path(os.environ.get("HYPERMIND_TEST_EMBEDDER_CACHE", "./data/models")).resolve()
REQUIRE_STACK = os.environ.get("HYPERMIND_REQUIRE_MEMORY_STACK") == "1"


def _stack_problem() -> str | None:
    import importlib.util

    # find_spec, not import: importing Mem0 runs its import-time side effects,
    # which only the adapter may trigger (after hardening the environment).
    if any(importlib.util.find_spec(name) is None for name in ("mem0", "chromadb", "fastembed")):
        return "the memory stack is not installed (pip install -e '.[dev,memory]')"
    from server.models.embedding import EmbedderUnavailable, LocalEmbedder

    try:
        LocalEmbedder(model="bge-small-en-v1.5", cache_dir=EMBEDDER_CACHE)
    except EmbedderUnavailable:
        return (f"embedding model not provisioned in {EMBEDDER_CACHE} "
                "(python -m server.memory provision --config config.example.yaml)")
    return None


def require_memory_stack() -> None:
    problem = _stack_problem()
    if problem is None:
        return
    if REQUIRE_STACK:
        pytest.fail(f"HYPERMIND_REQUIRE_MEMORY_STACK=1 but {problem}")
    pytest.skip(problem)


# Mem0 must never be imported with telemetry on — not even by this test module.
os.environ.setdefault("MEM0_TELEMETRY", "false")


# ── egress / disk guard ────────────────────────────────────────────────────


_NET_EVENTS = {
    "socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
    "urllib.Request", "http.client.connect", "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn",
}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


@dataclass
class EgressRecorder:
    allowed_write_roots: list[Path] = field(default_factory=list)
    events: list[tuple[str, str]] = field(default_factory=list)
    writes_outside: list[str] = field(default_factory=list)
    active: bool = False

    def network(self) -> list[tuple[str, str]]:
        return [e for e in self.events if e[0] != "subprocess.Popen"]

    def processes(self) -> list[tuple[str, str]]:
        return [e for e in self.events if e[0] in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"}]


_RECORDER: EgressRecorder | None = None
_HOOK_LOCK = threading.Lock()


def _hook(event: str, args: tuple) -> None:
    recorder = _RECORDER
    if recorder is None or not recorder.active:
        return
    if event in _NET_EVENTS:
        recorder.events.append((event, repr(args)[:160]))
    elif event == "open" and len(args) >= 3:
        path, mode, flags = args[0], args[1], args[2]
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & _WRITE_FLAGS
        )
        if writing and isinstance(path, (str, bytes, os.PathLike)):
            resolved = Path(os.fsdecode(path)).resolve()
            if not any(resolved == root or root in resolved.parents for root in recorder.allowed_write_roots):
                recorder.writes_outside.append(str(resolved))


sys.addaudithook(_hook)


@pytest.fixture
def egress(tmp_path) -> Any:
    global _RECORDER
    recorder = EgressRecorder(allowed_write_roots=[tmp_path.resolve()])
    with _HOOK_LOCK:
        _RECORDER = recorder
    try:
        yield recorder
    finally:
        recorder.active = False
        with _HOOK_LOCK:
            _RECORDER = None


# ── the real store ─────────────────────────────────────────────────────────


def mem0_section(root: Path, **overrides: Any):
    from server.config.schema import Mem0SectionConfig

    payload: dict[str, Any] = {"path": str(root / "mem0_storage"), "embedder_cache": str(EMBEDDER_CACHE)}
    payload.update(overrides)
    return Mem0SectionConfig(**payload)


@pytest.fixture
def mem0_provider(tmp_path):
    require_memory_stack()
    from server.memory.mem0_provider import build_mem0_provider

    return build_mem0_provider(mem0_section(tmp_path))


def vault_section(root: Path, repo: Path, **overrides: Any):
    from server.config.schema import VaultConfig

    payload = {"enabled": True, "path": str(repo), "index_path": str(root / "vault_index"),
               "embedder_cache": str(EMBEDDER_CACHE)}
    payload.update(overrides)
    return VaultConfig(**payload)


# ── a Git-backed vault ─────────────────────────────────────────────────────


def git(repo: Path, *args: str) -> str:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "HOME": str(repo)}
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=True)
    return done.stdout.strip()


def commit_files(repo: Path, files: dict[str, str | None], message: str = "vault change") -> str:
    for rel, text in files.items():
        target = repo / rel
        if text is None:
            git(repo, "rm", "-q", rel)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        git(repo, "add", rel)
    git(repo, "-c", "user.name=Vault Curator", "-c", "user.email=curator@example.test",
        "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


VAULT_FILES = {
    "product/units.md": "# Units\n\nJARVIS reports distances in kilometres unless the user asks otherwise.\n",
    "product/style.md": (
        "# Writing style\n\nAnswers are short, direct, and cite the source file when they rely on the vault.\n"
    ),
    "howto/backups.md": (
        "# Backups\n\nThe operator backs up the data directory nightly with restic to an offsite target.\n"
    ),
}


@pytest.fixture
def vault_repo(tmp_path) -> Path:
    repo = tmp_path / "vault_repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit_files(repo, VAULT_FILES, "initial curated content")
    return repo


@pytest.fixture
def vault_index(tmp_path, vault_repo):
    require_memory_stack()
    from server.vault.index import open_vault_index
    from server.vault.ingest import reindex

    config = vault_section(tmp_path, vault_repo)
    index = open_vault_index(config)
    reindex(index, repo=vault_repo, chunk_chars=config.chunk_chars)
    return index


# ── the full stack through the production composition root ────────────────


@pytest_asyncio.fixture
async def stack(make_harness, mem0_provider, tmp_path) -> Callable:
    async def _make(*, config: dict | None = None, vault_index: Any = None) -> Any:
        return await make_harness(config=config, memory_provider=mem0_provider, vault_index=vault_index)

    return _make


def unique(label: str) -> str:
    """A searchable, unmistakable marker for leak assertions."""

    return f"{label}-{uuid.uuid4().hex[:8]}"
