"""Knowledge Vault — 11 §7, docs/21 §5 (VAULT-001..005).

Static, shared, curated knowledge, kept apart from persistent memory in storage
and retrieval: its own Chroma client and directory (`vault.index_path`), its own
collection (`hypermind_vault`), and no import of `server.memory` or Mem0.

* `ingest` — Git-backed ingestion of the committed tree at `HEAD` (operator
  command: `python -m server.vault reindex`).
* `index` — the read side: `query` for `GET /api/v1/vault/query` and for
  runtime hydration, whose chunks reach a model only as untrusted data.

Pilot restriction (OD-VLT-1): content changes only through Git review and a
reindex. There is no HTTP write path.
"""

from server.vault.index import VaultIndex, VaultStatus, VaultUnavailable, open_vault_index

__all__ = ["VaultIndex", "VaultStatus", "VaultUnavailable", "open_vault_index"]
