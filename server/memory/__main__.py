"""Operator commands for persistent memory (15 §3 bootstrap).

    python -m server.memory provision [--config PATH]
        Download the configured embedding model(s) into their cache directories —
        the one sanctioned network step. The server itself only ever loads them
        offline (MP-T8). Covers `memory.mem0` and `vault`.

    python -m server.memory status [--config PATH]
        Open the configured store and report whether it answers.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from server.config import load_config
from server.models.embedding import provision_embedder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m server.memory")
    parser.add_argument("command", choices=["provision", "status"])
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)
    config = load_config(args.config)

    if args.command == "provision":
        targets = {
            (config.memory.mem0.embedder, config.memory.mem0.embedder_cache),
            (config.vault.embedder, config.vault.embedder_cache),
        }
        for model, cache in sorted(targets):
            path = provision_embedder(model=model, cache_dir=cache)
            print(f"provisioned {model} in {path}")
        return 0

    from server.memory.mem0_provider import build_mem0_provider

    provider = build_mem0_provider(config.memory.mem0)
    ok = asyncio.run(provider.health())
    print(f"memory store at {provider.path}: {'ok' if ok else 'unavailable'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
