"""`python -m experiments …` — the harness CLI (docs/31 §6).

    # build a snapshot offline from a profile document + an operator config
    python -m experiments make-snapshot --document profile.json --config config.yaml --out snap.json

    # run (scripted, offline — what CI does)
    python -m experiments run central --snapshot snap.json

    # run live against the providers named in a provider map (opt-in)
    python -m experiments run central --snapshot snap.json --live --providers providers.yaml

Results go to experiments/results/ (not committed) unless --no-write.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from experiments.providers import ProviderMap
from experiments.runner import RESULTS_DIR, RunConfig, offline_echo, rescore, run_sync
from experiments.snapshot import load_snapshot, make_snapshot

LIVE_ENV = "HYPERMIND_EXPERIMENTS_LIVE"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m experiments")
    sub = parser.add_subparsers(dest="command", required=True)

    mk = sub.add_parser("make-snapshot", help="build a snapshot from a profile document and an operator config")
    mk.add_argument("--document", type=Path, required=True)
    mk.add_argument("--config", type=Path, required=True)
    mk.add_argument("--out", type=Path, required=True)
    mk.add_argument("--profile-id", default="local")

    rn = sub.add_parser("run", help="run an experiment")
    rn.add_argument("experiment", choices=["central", "orchestrated", "synthesis"])
    rn.add_argument("--snapshot", type=Path, required=True)
    rn.add_argument("--live", action="store_true", help=f"call real providers (also needs {LIVE_ENV}=1)")
    rn.add_argument("--providers", type=Path, help="provider map YAML (required with --live)")
    rn.add_argument("--task", action="append", default=[], help="run only these task ids")
    rn.add_argument("--label", default="")
    rn.add_argument("--no-write", action="store_true")
    rs = sub.add_parser("rescore", help="re-score a stored run under the current rubric (no model calls)")
    rs.add_argument("directory", type=Path)
    args = parser.parse_args(argv)

    if args.command == "rescore":
        result = rescore(args.directory)
        print(json.dumps({"arm": result["arm"], **{k: result["summary"][k] for k in ("quality", "safety")}}, indent=2))
        return 0

    if args.command == "make-snapshot":
        snap = make_snapshot(args.document, args.config, profile_id=args.profile_id)
        args.out.write_text(json.dumps(snap.model_dump(mode="json"), indent=2))
        print(f"wrote {args.out} (config hash {snap.config_hash})")
        return 0

    snapshot = load_snapshot(args.snapshot)
    provider_map = None
    known: list[str] = []
    if args.live:
        if os.environ.get(LIVE_ENV) != "1":
            print(f"refusing a live run: set {LIVE_ENV}=1 to confirm calls to real providers", file=sys.stderr)
            return 2
        if args.providers is None:
            print("--live needs --providers", file=sys.stderr)
            return 2
        provider_map = ProviderMap.load(args.providers)
        known = [os.environ.get(e.api_key_env, "") for e in provider_map.providers.values() if e.api_key_env]
    report = run_sync(RunConfig(
        experiment=args.experiment, snapshot=snapshot, live=args.live, provider_map=provider_map,
        scripted=None if args.live else offline_echo, task_ids=tuple(args.task),
        out_dir=None if args.no_write else RESULTS_DIR, known_secret_values=tuple(known), label=args.label,
    ))
    print(json.dumps({k: report.summary[k] for k in ("run", "quality", "safety", "efficiency", "budget")}, indent=2))
    if report.directory:
        print(f"trace: {report.directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
