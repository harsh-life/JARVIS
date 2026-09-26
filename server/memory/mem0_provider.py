"""The Mem0 OSS adapter — the only module in JARVIS that imports `mem0`.

docs/21 §2, `[OWNER-RATIFIED]`: self-hosted Mem0 OSS, a pinned library
dependency, **not forked and not patched**. Everything JARVIS needs beyond
Mem0's defaults is wrapped here.

## Pinned version and what it does by default (inspected, mem0ai 2.2.1)

| Mem0 default | Network / disk effect | What this adapter does |
|---|---|---|
| `MEM0_TELEMETRY` unset → on | PostHog client built **at import**, events on every call, `~/.mem0/config.json` user id | `MEM0_TELEMETRY=false` set before the first import; refuses to start if Mem0 was already imported with it on |
| remote "notices" config | `urllib` fetch of a JSON file from GitHub | gated by the same flag — off |
| `MEM0_DIR` unset | creates `~/.mem0` at import | pointed inside `memory.mem0.path` |
| LLM `openai` | builds an OpenAI client; `add(infer=True)` calls it | no Mem0 LLM exists: a refusing stub; every write is `infer=False` (docs/21 §2.2 option (a)) |
| embedder `openai` | remote embedding API | JARVIS's offline `LocalEmbedder` (bge-small, `local_files_only`) |
| history SQLite | keeps old text of every UPDATE/DELETE on disk | not written: a null history sink — deletion leaves no plaintext copy there (MP-T11) |
| Chroma write-ahead log | keeps deleted/corrected text in `chroma.sqlite3` for up to 1000 operations | collection persists every 2 operations; each destructive call flushes the log and VACUUMs (`_erase_residue`) |
| spaCy (`[nlp]` extra) | downloads `en_core_web_sm` at runtime if spaCy is present; feeds a content-derived entity store | refuses to start if spaCy is importable |
| Chroma | `anonymized_telemetry` on | JARVIS builds the client with it off and hands it in |

Mem0's own constructor instantiates all of the above from one config object.
This adapter instead subclasses `Memory` and assigns the same attributes its
2.2.1 constructor assigns, with JARVIS's components. That depends on the pinned
version's attribute contract, so a different installed version is a startup
failure and `tests/memory/test_mem0_provider.py` exercises every call path.

## Scope mapping (docs/21 §2.3)

Every fact is one Mem0 record under scope `owner:{owner_user_id}` — its Mem0 id
*is* the fact id. Sharing writes a **mirror** under `graph:{graph_id}` (again
without inference); un-sharing deletes it. Search runs in the caller's own
scope plus one search per readable graph scope, with the visibility predicate
in each `where` clause, and every mirror hit is re-validated against its
canonical record before it is returned. Merging or deduplicating happens only
within one owner scope; text from two owners is never combined (MP-T3).
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import importlib.util
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from server.config.schema import Mem0SectionConfig
from server.memory.provider import MemoryCandidate, MemoryProviderUnavailable
from server.models.embedding import EmbedderUnavailable, LocalEmbedder
from shared.schemas.enums import FactType, Visibility
from shared.schemas.memory import Mem0Fact

logger = logging.getLogger("hypermind.memory.mem0")

MEM0_PINNED_VERSION = "2.2.1"

KIND_FACT = "fact"
KIND_MIRROR = "mirror"
_MAX_SCAN = 100_000

# Chroma keeps every write — text included — in its write-ahead log
# (`embeddings_queue`) until the vector segment next persists, which by default
# is every 1000 operations. A deleted or corrected fact's old text would sit in
# the database file until then. The memory collection therefore persists every
# two operations, and each destructive provider call ends with `_erase_residue`
# (a content-free sentinel write + delete to flush the log, then a VACUUM), so the
# removed text is gone from the file before the call returns.
# `test_deleted_and_corrected_text_leaves_no_trace_on_disk` byte-scans the store.
_HNSW_FLUSH = {"batch_size": 2, "sync_threshold": 2}
_SENTINEL_ID = "jarvis-log-flush"


class Mem0SetupError(Exception):
    """The Mem0 stack cannot be started safely. Raised at startup only; the
    message tells the operator what to change."""


def owner_scope(user_id: uuid.UUID) -> str:
    return f"owner:{user_id}"


def graph_scope(graph_id: uuid.UUID) -> str:
    return f"graph:{graph_id}"


def content_hash(fact_type: FactType, content: str) -> str:
    normalized = " ".join(content.split()).lower()
    return hashlib.sha256(f"{fact_type.value}\n{normalized}".encode()).hexdigest()


# ── import-time hardening ──────────────────────────────────────────────────


def _import_mem0(mem0_dir: Path) -> dict[str, Any]:
    """Import Mem0 with telemetry off and its home directory inside ours."""

    if importlib.util.find_spec("spacy") is not None:
        raise Mem0SetupError(
            "spaCy is installed in this environment. Mem0 would then download a spaCy model at "
            "runtime and keep a content-derived entity store outside JARVIS's deletion guarantees. "
            "Uninstall spaCy from the server's environment (docs/RUNNING_MEMORY.md)."
        )
    try:
        installed = importlib.metadata.version("mem0ai")
    except importlib.metadata.PackageNotFoundError:
        raise Mem0SetupError("the memory stack is not installed: pip install -e '.[memory]'") from None
    if installed != MEM0_PINNED_VERSION:
        raise Mem0SetupError(
            f"mem0ai {installed} is installed; this adapter is validated against "
            f"{MEM0_PINNED_VERSION} only (pyproject pin)"
        )

    os.environ["MEM0_TELEMETRY"] = "false"
    os.environ["MEM0_DIR"] = str(mem0_dir)

    import mem0.memory.telemetry as telemetry
    from mem0.configs.base import MemoryConfig
    from mem0.embeddings.base import EmbeddingBase
    from mem0.memory.main import Memory
    from mem0.vector_stores.chroma import ChromaDB

    if telemetry.MEM0_TELEMETRY:
        raise Mem0SetupError(
            "Mem0 was imported with telemetry enabled before the memory provider was built; "
            "set MEM0_TELEMETRY=false in the server environment"
        )
    # Mem0 logs memory text at INFO ("Updating memory with data=...").
    logging.getLogger("mem0").setLevel(logging.WARNING)
    return {
        "Memory": Memory,
        "MemoryConfig": MemoryConfig,
        "EmbeddingBase": EmbeddingBase,
        "ChromaDB": ChromaDB,
    }


class _RefusingLLM:
    """Mem0 never calls a model in this deployment (docs/21 §2.2 option (a)).
    Anything that would — `infer=True`, procedural memory, vision parsing — fails
    here instead of reaching an unmetered, undeclared provider (MP-T4)."""

    def generate_response(self, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError("Mem0 inference is disabled; JARVIS performs extraction itself")


class _NullHistory:
    """Mem0's history store, disabled. JARVIS's audit log is the record of
    memory changes and carries no fact text."""

    def add_history(self, *args: Any, **kwargs: Any) -> None:
        return None

    def batch_add_history(self, *args: Any, **kwargs: Any) -> None:
        return None

    def get_history(self, *args: Any, **kwargs: Any) -> list:
        return []

    def save_messages(self, *args: Any, **kwargs: Any) -> None:
        return None

    def get_last_messages(self, *args: Any, **kwargs: Any) -> list:
        return []

    def reset(self) -> None:
        return None

    def close(self) -> None:
        return None


def _build_memory(mods: dict[str, Any], *, client: Any, collection: str, embedder: LocalEmbedder) -> Any:
    EmbeddingBase = mods["EmbeddingBase"]
    Memory = mods["Memory"]

    class _JarvisEmbedding(EmbeddingBase):
        def __init__(self) -> None:
            super().__init__(None)
            self.config.embedding_dims = embedder.dimensions
            self.config.model = embedder.model_name

        def embed(self, text, memory_action=None):  # noqa: ANN001 — Mem0's signature
            return embedder.embed(text)

        def embed_batch(self, texts, memory_action="add"):  # noqa: ANN001
            return embedder.embed_many(list(texts))

    class _JarvisMemory(Memory):
        """`Memory` with JARVIS-owned components. Mirrors the attribute set
        `Memory.__init__` assigns in mem0ai 2.2.1, minus telemetry."""

        def __init__(self) -> None:  # noqa: D401 — deliberately not calling super()
            self.config = mods["MemoryConfig"].model_construct()
            self.embedding_model = _JarvisEmbedding()
            self.vector_store = mods["ChromaDB"](collection_name=collection, client=client)
            self.llm = _RefusingLLM()
            self.db = _NullHistory()
            self.collection_name = collection
            self.api_version = "v1.1"
            self.custom_instructions = None
            self.reranker = None
            self._entity_store = None

    return _JarvisMemory()


def _private_dir(path: Path) -> Path:
    # docs/21 §7: no at-rest encryption yet — filesystem permissions are the
    # protection, so the store is created owner-only.
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


# ── record ↔ Mem0Fact ──────────────────────────────────────────────────────


def _payload_of(item: Any) -> tuple[str | None, str | None, dict]:
    """(id, text, metadata) from either a Mem0 result dict or a vector-store row."""

    if isinstance(item, dict):
        meta = dict(item.get("metadata") or {})
        for key in ("user_id",):
            if key in item:
                meta[key] = item[key]
        return item.get("id"), item.get("memory"), meta
    payload = dict(getattr(item, "payload", None) or {})
    return getattr(item, "id", None), payload.get("data"), payload


def _fact_from(record_id: str, text: str | None, meta: dict) -> Mem0Fact | None:
    """Fail-closed projection: anything malformed is treated as absent."""

    try:
        if meta.get("jarvis_kind") != KIND_FACT or not text:
            return None
        return Mem0Fact(
            fact_id=uuid.UUID(str(record_id)),
            owner_user_id=uuid.UUID(meta["owner_user_id"]),
            source_user_id=uuid.UUID(meta["source_user_id"]),
            graph_id=uuid.UUID(meta["graph_id"]),
            visibility=Visibility(meta["visibility"]),
            fact_type=FactType(meta["fact_type"]),
            content=text,
            timestamp=datetime.fromisoformat(meta["timestamp"]),
            source_session_id=(
                uuid.UUID(meta["source_session_id"]) if meta.get("source_session_id") else None
            ),
            embedding_ref=str(record_id),
        )
    except (KeyError, ValueError, TypeError):
        return None


class Mem0MemoryProvider:
    """`MemoryProvider` over a local, self-hosted Mem0 + Chroma store.

    Mem0's API is synchronous and its Chroma store is a single local database, so
    every call runs in a worker thread under one lock. That also makes each
    provider method's multi-step writes (mirror, then canonical) atomic with
    respect to one another inside this process.
    """

    def __init__(self, *, memory: Any, client: Any, collection: str, path: Path) -> None:
        self._mem = memory
        self._client = client
        self.collection = collection
        self.path = path
        self._lock = threading.Lock()

    # Exposed for the separation tests (MP-T9) and the BR-T2 measurement.
    @property
    def client(self) -> Any:
        return self._client

    @property
    def mem0(self) -> Any:
        return self._mem

    async def _run(self, op: str, fn: Callable[[], Any]) -> Any:
        def locked() -> Any:
            with self._lock:
                return fn()

        try:
            return await asyncio.to_thread(locked)
        except MemoryProviderUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 — a store failure degrades (FAIL-008)
            # The backend's message may quote stored text; only the type is logged.
            logger.warning("mem0 %s failed (%s)", op, type(exc).__name__)
            raise MemoryProviderUnavailable(op) from None

    # ── internal, called under the lock ─────────────────────────────────

    def _rows(self, filters: dict) -> list[Any]:
        rows: list[Any] = []
        listed = self._mem.vector_store.list(filters=filters, top_k=_MAX_SCAN)
        batch = listed[0] if listed and isinstance(listed[0], list) else listed
        rows.extend(batch or [])
        return rows

    def _canonical(self, fact_id: str) -> Mem0Fact | None:
        got = self._mem.get(fact_id)
        if not got:
            return None
        record_id, text, meta = _payload_of(got)
        fact = _fact_from(record_id or fact_id, text, meta)
        if fact is None:
            return None
        # The record must sit in its owner's own scope; anything else is not a
        # canonical fact, whatever its metadata claims.
        if meta.get("user_id") != owner_scope(fact.owner_user_id):
            return None
        return fact

    def _mirrors(self, fact_id: str) -> list[Any]:
        return self._rows({"jarvis_kind": KIND_MIRROR, "mirror_of": fact_id})

    def _mirror_metadata(self, fact: Mem0Fact) -> dict:
        return {
            "jarvis_kind": KIND_MIRROR,
            "mirror_of": str(fact.fact_id),
            "owner_user_id": str(fact.owner_user_id),
            "source_user_id": str(fact.source_user_id),
            "graph_id": str(fact.graph_id),
            "visibility": Visibility.GRAPH.value,
            "fact_type": fact.fact_type.value,
            "timestamp": fact.timestamp.isoformat(),
            "content_hash": content_hash(fact.fact_type, fact.content),
        }

    def _erase_residue(self) -> None:
        """Make a deletion or correction physical, not just logical.

        1. Flush Chroma's write-ahead log with a content-free sentinel write +
           delete, so the log entries holding removed text are purged
           (see `_HNSW_FLUSH`).
        2. `VACUUM` the store's SQLite file. Chroma's own connection does not
           enable `secure_delete`, so removed rows otherwise survive as bytes in
           freed pages. VACUUM rewrites the file without them.

        Vectors of removed facts can remain in Chroma's HNSW files until the
        index is rebuilt; they are not text, and docs/OD_A1_BR_T2.md records them
        as an at-rest residual.
        """

        collection = self._mem.vector_store.collection
        dims = self._mem.embedding_model.config.embedding_dims
        collection.upsert(ids=[_SENTINEL_ID], embeddings=[[0.0] * dims], metadatas=[{"jarvis_kind": "flush"}])
        collection.delete(ids=[_SENTINEL_ID])
        database = self.path / "chroma" / "chroma.sqlite3"
        if database.exists():
            connection = sqlite3.connect(database, timeout=30)
            try:
                connection.execute("VACUUM")
            finally:
                connection.close()

    def _delete_rows(self, rows: Sequence[Any]) -> int:
        deleted = 0
        for row in rows:
            row_id = getattr(row, "id", None)
            if row_id is None:
                continue
            try:
                self._mem.delete(str(row_id))
                deleted += 1
            except ValueError:
                continue  # already gone
        return deleted

    # ── MemoryProvider ──────────────────────────────────────────────────

    async def add(self, fact: Mem0Fact) -> uuid.UUID:
        digest = content_hash(fact.fact_type, fact.content)
        scope = owner_scope(fact.owner_user_id)
        metadata = {
            "jarvis_kind": KIND_FACT,
            "owner_user_id": str(fact.owner_user_id),
            "source_user_id": str(fact.source_user_id),
            "graph_id": str(fact.graph_id),
            # RAUTH-005: stored private, whatever the caller passed.
            "visibility": Visibility.PRIVATE.value,
            "fact_type": fact.fact_type.value,
            "timestamp": fact.timestamp.astimezone(timezone.utc).isoformat(),
            "content_hash": digest,
        }
        if fact.source_session_id is not None:
            metadata["source_session_id"] = str(fact.source_session_id)

        def op() -> uuid.UUID:
            # docs/21 §4 step 5: an exact duplicate within the owner's own scope
            # (same type, same normalized text) is the existing fact.
            existing = self._rows(
                {"user_id": scope, "jarvis_kind": KIND_FACT, "content_hash": digest,
                 "owner_user_id": str(fact.owner_user_id)}
            )
            if existing:
                return uuid.UUID(str(existing[0].id))
            result = self._mem.add(fact.content, user_id=scope, metadata=metadata, infer=False)
            records = result.get("results") or []
            if len(records) != 1:
                raise RuntimeError("unexpected add result")
            return uuid.UUID(str(records[0]["id"]))

        return await self._run("add", op)

    async def get(self, fact_id: uuid.UUID) -> Mem0Fact | None:
        return await self._run("get", lambda: self._canonical(str(fact_id)))

    async def search(
        self,
        *,
        query: str,
        owner_user_id: uuid.UUID,
        readable_graph_ids: frozenset[uuid.UUID],
        limit: int,
    ) -> Sequence[MemoryCandidate]:
        if limit <= 0 or not query.strip():
            return []

        def op() -> list[MemoryCandidate]:
            hits: dict[str, MemoryCandidate] = {}
            own = self._mem.search(
                query,
                top_k=limit,
                threshold=0.0,
                filters={"user_id": owner_scope(owner_user_id), "jarvis_kind": KIND_FACT,
                         "owner_user_id": str(owner_user_id)},
            )
            for item in own.get("results") or []:
                record_id, text, meta = _payload_of(item)
                fact = _fact_from(record_id, text, meta)
                if fact is None or fact.owner_user_id != owner_user_id:
                    continue
                hits[str(fact.fact_id)] = _candidate(fact, float(item.get("score") or 0.0))

            for graph_id in sorted(readable_graph_ids, key=str):
                shared = self._mem.search(
                    query,
                    top_k=limit,
                    threshold=0.0,
                    filters={"user_id": graph_scope(graph_id), "jarvis_kind": KIND_MIRROR,
                             "visibility": Visibility.GRAPH.value, "graph_id": str(graph_id)},
                )
                for item in shared.get("results") or []:
                    _, _, meta = _payload_of(item)
                    canonical_id = meta.get("mirror_of")
                    if not canonical_id or canonical_id in hits:
                        continue
                    # The canonical record decides; a stale mirror is never trusted.
                    fact = self._canonical(canonical_id)
                    if fact is None or fact.visibility is not Visibility.GRAPH or fact.graph_id != graph_id:
                        continue
                    hits[canonical_id] = _candidate(fact, float(item.get("score") or 0.0))

            ranked = sorted(hits.values(), key=lambda c: c.score, reverse=True)
            return ranked[:limit]

        return await self._run("search", op)

    async def list_facts(
        self,
        *,
        owner_user_id: uuid.UUID,
        readable_graph_ids: frozenset[uuid.UUID],
        limit: int,
    ) -> Sequence[Mem0Fact]:
        if limit <= 0:
            return []

        def op() -> list[Mem0Fact]:
            facts: dict[str, Mem0Fact] = {}
            for row in self._rows({"user_id": owner_scope(owner_user_id), "jarvis_kind": KIND_FACT,
                                   "owner_user_id": str(owner_user_id)}):
                record_id, text, meta = _payload_of(row)
                fact = _fact_from(record_id, text, meta)
                if fact is not None and fact.owner_user_id == owner_user_id:
                    facts[str(fact.fact_id)] = fact
            for graph_id in sorted(readable_graph_ids, key=str):
                for row in self._rows({"user_id": graph_scope(graph_id), "jarvis_kind": KIND_MIRROR,
                                       "visibility": Visibility.GRAPH.value, "graph_id": str(graph_id)}):
                    _, _, meta = _payload_of(row)
                    canonical_id = meta.get("mirror_of")
                    if not canonical_id or canonical_id in facts:
                        continue
                    fact = self._canonical(canonical_id)
                    if fact is not None and fact.visibility is Visibility.GRAPH and fact.graph_id == graph_id:
                        facts[canonical_id] = fact
            ordered = sorted(facts.values(), key=lambda f: f.timestamp, reverse=True)
            return ordered[:limit]

        return await self._run("list", op)

    async def update_content(self, fact_id: uuid.UUID, content: str) -> None:
        def op() -> None:
            fact = self._canonical(str(fact_id))
            if fact is None:
                raise MemoryProviderUnavailable("update")
            digest = content_hash(fact.fact_type, content)
            self._mem.update(str(fact_id), text=content, metadata={"content_hash": digest})
            # The owner's own mirrors carry the owner's own corrected text — the
            # only text a mirror ever holds (MP-T3).
            for mirror in self._mirrors(str(fact_id)):
                self._mem.update(str(mirror.id), text=content, metadata={"content_hash": digest})
            self._erase_residue()

        await self._run("update", op)

    async def set_visibility(self, fact_id: uuid.UUID, visibility: Visibility) -> None:
        def op() -> None:
            fact = self._canonical(str(fact_id))
            if fact is None:
                raise MemoryProviderUnavailable("set_visibility")
            if visibility is Visibility.GRAPH:
                # Mirror first, then the canonical flag: a failure in between
                # leaves the fact private and the orphan mirror is ignored by
                # search (the canonical record decides).
                if not self._mirrors(str(fact_id)):
                    self._mem.add(fact.content, user_id=graph_scope(fact.graph_id),
                                  metadata=self._mirror_metadata(fact), infer=False)
                self._mem.update(str(fact_id), metadata={"visibility": Visibility.GRAPH.value})
            else:
                # Un-share: remove every mirror before flipping the flag, so no
                # instant exists where a graph-scope copy outlives the decision.
                self._delete_rows(self._mirrors(str(fact_id)))
                self._mem.update(str(fact_id), metadata={"visibility": Visibility.PRIVATE.value})
                self._erase_residue()

        await self._run("set_visibility", op)

    async def delete(self, fact_id: uuid.UUID) -> bool:
        def op() -> bool:
            fact = self._canonical(str(fact_id))
            self._delete_rows(self._mirrors(str(fact_id)))
            if fact is None:
                self._erase_residue()
                return False
            self._mem.delete(str(fact_id))
            self._erase_residue()
            return True

        return await self._run("delete", op)

    async def delete_all_for_user(self, user_id: uuid.UUID) -> int:
        def op() -> int:
            # Every record carrying this owner — canonical facts and their
            # mirrors in any graph scope.
            removed = self._delete_rows(self._rows({"owner_user_id": str(user_id)}))
            self._erase_residue()
            return removed

        return await self._run("delete_all_for_user", op)

    async def delete_graph_shared(self, graph_id: uuid.UUID) -> int:
        def op() -> int:
            # Graph-visible canonical facts of this graph and their mirrors.
            # Private facts formed in the graph are not touched (MEM-T9).
            rows = self._rows({"graph_id": str(graph_id), "visibility": Visibility.GRAPH.value})
            removed = self._delete_rows(rows)
            self._erase_residue()
            return removed

        return await self._run("delete_graph_shared", op)

    async def health(self) -> bool:
        try:
            await self._run("health", lambda: self._mem.vector_store.collection.count())
        except MemoryProviderUnavailable:
            return False
        return True


def _candidate(fact: Mem0Fact, score: float) -> MemoryCandidate:
    return MemoryCandidate(
        fact_id=str(fact.fact_id),
        content=fact.content,
        owner_user_id=fact.owner_user_id,
        visibility=fact.visibility,
        graph_id=fact.graph_id,
        score=score,
    )


def build_mem0_provider(section: Mem0SectionConfig) -> Mem0MemoryProvider:
    """Open (or create) the self-hosted store described by `memory.mem0`.

    Raises `Mem0SetupError` / `EmbedderUnavailable` — startup failures — when the
    stack is missing, the wrong version, unsafe (spaCy), or not provisioned.
    """

    root = _private_dir(Path(section.path))
    mods = _import_mem0(_private_dir(root / "mem0_home"))
    embedder = LocalEmbedder(model=section.embedder, cache_dir=section.embedder_cache)

    import chromadb
    from chromadb.config import Settings

    # This client object belongs to persistent memory alone. The vault builds
    # its own, on its own directory (VAULT-003, MP-T9).
    client = chromadb.PersistentClient(
        path=str(_private_dir(root / "chroma")),
        settings=Settings(anonymized_telemetry=False, allow_reset=False),
    )
    # Created here, before Mem0 opens it, so it carries the flush settings the
    # deletion guarantee depends on (see `_HNSW_FLUSH`). An existing store is
    # brought to the same settings.
    collection = client.get_or_create_collection(
        name=section.collection, embedding_function=None, configuration={"hnsw": dict(_HNSW_FLUSH)}
    )
    hnsw = (collection.configuration_json or {}).get("hnsw") or {}
    if any(hnsw.get(key) != value for key, value in _HNSW_FLUSH.items()):
        collection.modify(configuration={"hnsw": dict(_HNSW_FLUSH)})
    memory = _build_memory(mods, client=client, collection=section.collection, embedder=embedder)
    return Mem0MemoryProvider(memory=memory, client=client, collection=section.collection, path=root)


__all__ = [
    "EmbedderUnavailable",
    "KIND_FACT",
    "KIND_MIRROR",
    "MEM0_PINNED_VERSION",
    "Mem0MemoryProvider",
    "Mem0SetupError",
    "build_mem0_provider",
    "content_hash",
    "graph_scope",
    "owner_scope",
]
