"""Operator commands for the Knowledge Vault (docs/21 §5).

    python -m server.vault reindex [--config PATH]
        Index the committed tree at HEAD of `vault.path` into the vault's own
        store. Run after a reviewed change is committed (MP-T10).

    python -m server.vault status [--config PATH]
        Report the indexed commit against the repository's HEAD.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from server.config import load_config
from server.vault.index import open_vault_index
from server.vault.ingest import head_commit, reindex


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m server.vault")
    parser.add_argument("command", choices=["reindex", "status"])
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    index = open_vault_index(config.vault)
    repo = Path(config.vault.path)

    if args.command == "reindex":
        report = reindex(index, repo=repo, chunk_chars=config.vault.chunk_chars)
        print(f"indexed commit {report.commit}: {report.files_indexed} file(s), {report.chunks_total} chunk(s) "
              f"({report.chunks_embedded} embedded, {report.chunks_removed} removed)")
        for path, reason in report.refused:
            print(f"  refused {path}: {reason}")
        return 0

    status = asyncio.run(index.status())
    head = head_commit(repo)
    fresh = "current" if status.indexed_commit == head else "STALE — run reindex"
    print(f"vault index: {status.chunks} chunk(s), indexed {status.indexed_commit}, HEAD {head} ({fresh})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
