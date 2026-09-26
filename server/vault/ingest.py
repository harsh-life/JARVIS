"""Git-backed vault ingestion (docs/21 §5, MP-T10, OD-VLT-1 pilot reading).

The source of truth is a Git repository of curated markdown (`vault.path`). A
reindex reads **the committed tree at `HEAD` only** — straight from Git's object
store, never the working directory — so an uncommitted, unreviewed edit can
never reach the index, and every indexed chunk traces to a commit.

This runs as an operator command (`python -m server.vault reindex`), after a
reviewed change is committed. There is no HTTP write path: `POST
/api/v1/vault/documents` stays unimplemented for the pilot.

Refused, per file, and reported by path and reason (never by content):

* a domain outside VAULT-005's MVP scope (`finance`, `health`, `education`);
* a file containing secret-shaped text (same detector as the memory gate) —
  curated knowledge must never carry a credential (12 §6);
* non-UTF-8, oversized, or non-regular (symlink, submodule) entries.

Git is read in-process with dulwich (a pure-Python Git implementation): this
module only reads the object store, spawns no `git` process, and never contacts
a remote — process and socket primitives stay confined to the packages
chartered to mediate them (`server/execution`, `server/net`).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from server.security.secret_patterns import find_secret
from server.vault.index import VaultIndex
from shared.schemas.memory import VaultDocument

# VAULT-005: no finance/health/education domain content in the MVP vault.
FORBIDDEN_DOMAINS = frozenset({"finance", "health", "education"})
DEFAULT_DOMAIN = "general"
MAX_FILE_BYTES = 1_000_000
_NAMESPACE = uuid.UUID("0b1d5c4e-7a52-4f8e-9f1e-6c2d9a3b7e10")
_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,62}$")


class VaultIngestError(Exception):
    """The reindex could not run at all (not a Git repository, or no commit)."""


@dataclass
class ReindexReport:
    commit: str
    files_indexed: int = 0
    chunks_total: int = 0
    chunks_embedded: int = 0
    chunks_removed: int = 0
    refused: list[tuple[str, str]] = field(default_factory=list)


_REGULAR_MODES = {0o100644, 0o100755}


def _open(repo: Path):
    from dulwich.errors import NotGitRepository
    from dulwich.repo import Repo

    try:
        return Repo(str(repo))
    except (NotGitRepository, FileNotFoundError, NotADirectoryError):
        raise VaultIngestError(f"{repo} is not a Git repository") from None


def _head(repository) -> bytes:
    try:
        return repository.head()
    except KeyError:
        raise VaultIngestError("the vault repository has no commit yet") from None


def head_commit(repo: Path) -> str:
    repository = _open(repo)
    try:
        return _head(repository).decode()
    finally:
        repository.close()


def _tree(repository) -> list[tuple[int, bytes, str]]:
    """(mode, blob sha, path) for every entry of the committed tree at HEAD."""

    from dulwich.object_store import iter_tree_contents

    commit = repository[_head(repository)]
    return [
        (entry.mode, entry.sha, entry.path.decode("utf-8", "replace"))
        for entry in iter_tree_contents(repository.object_store, commit.tree)
    ]


def chunk_markdown(text: str, *, max_chars: int) -> list[str]:
    """Split on headings and blank lines, packing paragraphs up to `max_chars`."""

    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#") and current:
            blocks.append("\n".join(current).strip())
            current = []
        current.append(line)
        if not line.strip() and current:
            blocks.append("\n".join(current).strip())
            current = []
    if current:
        blocks.append("\n".join(current).strip())

    chunks: list[str] = []
    buf = ""
    for block in (b for b in blocks if b):
        while len(block) > max_chars:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(block[:max_chars])
            block = block[max_chars:]
        if buf and len(buf) + 2 + len(block) > max_chars:
            chunks.append(buf)
            buf = block
        else:
            buf = f"{buf}\n\n{block}" if buf else block
    if buf:
        chunks.append(buf)
    return chunks


def _domain_of(path: str) -> str:
    parts = path.split("/")
    return parts[0].lower() if len(parts) > 1 else DEFAULT_DOMAIN


def collect_documents(repo: Path, *, chunk_chars: int) -> tuple[str, list[tuple[VaultDocument, str]], list[tuple[str, str]]]:
    """(commit, [(document, blob sha)], refused) for the committed tree at HEAD."""

    repository = _open(repo)
    try:
        commit = _head(repository).decode()
        documents: list[tuple[VaultDocument, str]] = []
        refused: list[tuple[str, str]] = []
        for mode, raw_sha, path in _tree(repository):
            if not path.lower().endswith(".md"):
                continue
            if mode not in _REGULAR_MODES:
                refused.append((path, "not_a_regular_file"))  # symlink, submodule
                continue
            domain = _domain_of(path)
            if not _DOMAIN_RE.match(domain):
                refused.append((path, "invalid_domain"))
                continue
            if domain in FORBIDDEN_DOMAINS:
                refused.append((path, "forbidden_domain"))
                continue
            blob = repository[raw_sha].data
            if len(blob) > MAX_FILE_BYTES:
                refused.append((path, "too_large"))
                continue
            try:
                text = blob.decode("utf-8")
            except UnicodeDecodeError:
                refused.append((path, "not_utf8"))
                continue
            pattern = find_secret(text)
            if pattern is not None:
                refused.append((path, f"secret_detected:{pattern}"))
                continue
            sha = raw_sha.decode()
            for index, chunk in enumerate(chunk_markdown(text, max_chars=chunk_chars)):
                doc = VaultDocument(
                    doc_id=uuid.uuid5(_NAMESPACE, f"{sha}:{path}:{index}"),
                    source_file=path, domain=domain, chunk=chunk,
                )
                documents.append((doc, sha))
        return commit, documents, refused
    finally:
        repository.close()


def reindex(index: VaultIndex, *, repo: Path, chunk_chars: int) -> ReindexReport:
    """Make the index equal the committed tree: embed new chunks, drop stale ones."""

    commit, documents, refused = collect_documents(repo, chunk_chars=chunk_chars)
    report = ReindexReport(commit=commit, refused=refused)
    existing = index.existing_ids()
    keep: set[str] = set()
    to_embed: list[tuple[VaultDocument, str]] = []
    files: set[str] = set()
    for doc, sha in documents:
        doc_id = str(doc.doc_id)
        keep.add(doc_id)
        files.add(doc.source_file)
        if doc_id not in existing:
            to_embed.append((doc, sha))
    vectors = index.embedder.embed_many([doc.chunk for doc, _ in to_embed])
    upserts = [
        (str(doc.doc_id), doc.chunk,
         {"source_file": doc.source_file, "domain": doc.domain, "blob": sha, "commit": commit},
         vector)
        for (doc, sha), vector in zip(to_embed, vectors)
    ]
    report.chunks_removed = index.replace_chunks(keep_ids=keep, upserts=upserts)
    index.record_commit(commit)
    report.files_indexed = len(files)
    report.chunks_total = len(keep)
    report.chunks_embedded = len(upserts)
    return report


__all__ = [
    "FORBIDDEN_DOMAINS",
    "ReindexReport",
    "VaultIngestError",
    "chunk_markdown",
    "collect_documents",
    "head_commit",
    "reindex",
]
