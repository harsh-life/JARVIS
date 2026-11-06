"""`python -m experiments …` — the harness CLI (docs/31 §6).

    # build a snapshot offline from a profile document + an operator config
    python -m experiments make-snapshot --document profile.json --config config.yaml --out snap.json

    # run (scripted, offline — what CI does)
    python -m experiments run central --snapshot snap.json

    # run live against the providers named in a provider map (opt-in)
    python -m experiments run central --snapshot snap.json --live --providers providers.yaml

    # Phase 0.1: re-finalize stored runs and report axes A/B/C (no model calls)
    python -m experiments compare results/<run-a> results/<run-b> --out results/compare-<label>

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
    cp = sub.add_parser("compare", help="re-finalize stored runs and write the A/B/C comparison to a NEW directory "
                                        "(no model calls; run directories are never modified)")
    cp.add_argument("directories", type=Path, nargs="+")
    cp.add_argument("--out", type=Path, required=True)
    se = sub.add_parser("screen-eval", help="false-positive / false-negative rates of the lexical claim screen "
                                            "on the held-out corpus (no model calls)")
    se.add_argument("--out", type=Path)
    ie = sub.add_parser("intent-eval", help="score action-intent recognition (deterministic offline; with --live "
                                           "also the model and hybrid recognizers)")
    ie.add_argument("--live", action="store_true")
    ie.add_argument("--snapshot", type=Path, help="snapshot naming the model (required with --live)")
    ie.add_argument("--providers", type=Path)
    ie.add_argument("--replay", type=Path, help="score a stored live run directory instead of calling a model")
    ie.add_argument("--out", type=Path, required=True)
    ie.add_argument("--label", default="")
    args = parser.parse_args(argv)

    if args.command == "intent-eval":
        from experiments import intent_eval

        if args.live:
            if os.environ.get(LIVE_ENV) != "1" or args.providers is None or args.snapshot is None:
                print(f"--live needs {LIVE_ENV}=1, --providers and --snapshot", file=sys.stderr)
                return 2
            run_dir = intent_eval.run_live(load_snapshot(args.snapshot), ProviderMap.load(args.providers),
                                           RESULTS_DIR, label=args.label)
            print(f"raw model outputs: {run_dir}")
            args.replay = run_dir
        if args.replay is not None:
            intent_eval.replay(args.replay, args.out)
        else:
            args.out.mkdir(parents=True, exist_ok=False)
            for suite in intent_eval.SUITES:
                rep = intent_eval.evaluate(None, suite=suite)
                (args.out / f"{suite}.md").write_text(intent_eval.render_markdown(rep))
                (args.out / f"{suite}.json").write_text(json.dumps(rep, indent=2))
        print(f"wrote {args.out}")
        return 0

    if args.command == "screen-eval":
        from experiments.screen_eval import main as screen_eval

        result = screen_eval(args.out)
        print(json.dumps({k: result[k] for k in ("items", "overall", "by_policy")}, indent=2))
        return 0

    if args.command == "compare":
        from experiments.compare import compare

        report = compare(list(args.directories), args.out)
        print(f"wrote {args.out}/compare.md and compare.json ({len(report['runs'])} runs)")
        return 0

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
