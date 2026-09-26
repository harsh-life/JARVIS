"""The Knowledge Vault's retrieval index (11 §7, docs/21 §5, VAULT-001..005).

Separate from persistent memory in every way that could merge them
(`[LOCKED]` VAULT-003, MP-T9):

* its **own Chroma client object**, opened on its **own directory**
  (`vault.index_path`; config validation refuses a path shared with or nested in
  `memory.mem0.path`), holding its own collection (`hypermind_vault`);
* no import of `server.memory` and no Mem0 — the vault is not a memory scope;
* no per-user data and no visibility triplet: vault text is shared-curated by
  construction, and every chunk is untrusted *data* when it reaches a model
  (PRD §24), however curated.

The index is derived state. Git is the source of truth (`server/vault/ingest.py`);
nothing is written here except by a reindex of committed content, so no vault
text lives only in the vector store.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server.config.schema import VaultConfig
from server.models.embedding import LocalEmbedder
from shared.schemas.memory import VaultQueryResultItem

logger = logging.getLogger("hypermind.vault.index")

_STATE_FILE = "indexed_commit.json"


class VaultUnavailable(Exception):
    """The vault index cannot answer (FAIL-009). Carries no content."""


@dataclass(frozen=True)
class VaultStatus:
    available: bool
    indexed_commit: str | None
    chunks: int


class VaultIndex:
    def __init__(self, *, config: VaultConfig, embedder: LocalEmbedder, client: Any) -> None:
        self._config = config
        self._embedder = embedder
        self._client = client
        self._lock = threading.Lock()
        # Cosine space, so a relevance score is a similarity in [0, 1].
        self._collection = client.get_or_create_collection(
            name=config.collection, embedding_function=None, metadata={"hnsw:space": "cosine"}
        )
        self.path = Path(config.index_path)

    @property
    def client(self) -> Any:
        return self._client

    @property
    def collection(self) -> Any:
        return self._collection

    @property
    def embedder(self) -> LocalEmbedder:
        return self._embedder

    def indexed_commit(self) -> str | None:
        try:
            return json.loads((self.path / _STATE_FILE).read_text())["commit"]
        except (OSError, ValueError, KeyError):
            return None

    def record_commit(self, commit: str) -> None:
        state = self.path / _STATE_FILE
        tmp = state.with_suffix(".tmp")
        tmp.write_text(json.dumps({"commit": commit}))
        os.replace(tmp, state)

    async def query(self, *, question: str, domain: str | None, top_k: int) -> list[VaultQueryResultItem]:
        question = question.strip()
        if not question or top_k <= 0:
            return []

        def op() -> list[VaultQueryResultItem]:
            with self._lock:
                vector = self._embedder.embed(question)
                where = {"domain": domain} if domain else None
                count = self._collection.count()
                if count == 0:
                    return []
                result = self._collection.query(
                    query_embeddings=[vector], n_results=min(top_k, count), where=where,
                    include=["documents", "metadatas", "distances"],
                )
            items: list[VaultQueryResultItem] = []
            docs = (result.get("documents") or [[]])[0]
            metas = (result.get("metadatas") or [[]])[0]
            dists = (result.get("distances") or [[]])[0]
            for doc, meta, dist in zip(docs, metas, dists):
                if not doc or not isinstance(meta, dict):
                    continue
                relevance = max(0.0, min(1.0, 1.0 - float(dist)))
                items.append(VaultQueryResultItem(chunk=doc, source_file=str(meta.get("source_file", "")),
                                                  relevance_score=relevance))
            return items

        try:
            return await asyncio.to_thread(op)
        except Exception as exc:  # noqa: BLE001 — a vault failure degrades (FAIL-009)
            logger.warning("vault query failed (%s)", type(exc).__name__)
            raise VaultUnavailable("query") from None

    async def status(self) -> VaultStatus:
        def op() -> int:
            with self._lock:
                return int(self._collection.count())

        try:
            chunks = await asyncio.to_thread(op)
        except Exception:  # noqa: BLE001
            return VaultStatus(available=False, indexed_commit=None, chunks=0)
        return VaultStatus(available=True, indexed_commit=self.indexed_commit(), chunks=chunks)

    # Reindex support, used only by `server/vault/ingest.py` under this lock.
    def replace_chunks(self, *, keep_ids: set[str], upserts: list[tuple[str, str, dict, list[float]]]) -> int:
        with self._lock:
            existing = set(self._collection.get(include=[])["ids"])
            stale = sorted(existing - keep_ids)
            if stale:
                self._collection.delete(ids=stale)
            if upserts:
                self._collection.upsert(
                    ids=[u[0] for u in upserts],
                    documents=[u[1] for u in upserts],
                    metadatas=[u[2] for u in upserts],
                    embeddings=[u[3] for u in upserts],
                )
            return len(stale)

    def existing_ids(self) -> set[str]:
        with self._lock:
            return set(self._collection.get(include=[])["ids"])


def open_vault_index(config: VaultConfig) -> VaultIndex:
    """Open the vault's own store. Raises `EmbedderUnavailable` when the model
    is not provisioned (a startup failure when the vault is enabled)."""

    import chromadb
    from chromadb.config import Settings

    path = Path(config.index_path)
    path.mkdir(parents=True, exist_ok=True)
    embedder = LocalEmbedder(model=config.embedder, cache_dir=config.embedder_cache)
    # A client object of the vault's own, on the vault's own directory — never
    # the memory store's client (VAULT-003).
    client = chromadb.PersistentClient(
        path=str(path / "chroma"), settings=Settings(anonymized_telemetry=False, allow_reset=False)
    )
    return VaultIndex(config=config, embedder=embedder, client=client)


__all__ = ["VaultIndex", "VaultStatus", "VaultUnavailable", "open_vault_index"]
