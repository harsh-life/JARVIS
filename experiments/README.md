# experiments/ — the isolated intelligence experiment harness

Not production: see `docs/31_INTELLIGENCE_EXPERIMENTS.md`. Nothing in `server/`
imports this package, and it reaches no production state. Synthetic tasks only.

```bash
python -m experiments make-snapshot --document profile.json --config config.yaml --out snap.json
python -m experiments run central      --snapshot snap.json   # E0/E1 (by the snapshot's Central prompt profile)
python -m experiments run orchestrated --snapshot snap.json   # E2/E3 (by the snapshot's backend)
python -m experiments run synthesis    --snapshot snap.json   # E4
# add --live --providers providers.yaml and HYPERMIND_EXPERIMENTS_LIVE=1 for real calls
```

Layout: `contracts.py` (WorkPackage, ResultBundle, typed bodies, Plan) ·
`accounting.py` (per-call metering, reserve-before-dispatch) · `providers.py`
(provider map, scripted providers) · `prompts.py` + `prompt_profiles/` ·
`central.py` (E0/E1, synthesis) · `specialists.py` · `orchestration/` (ports,
validator, backends, dispatcher) · `tasks/` (suites) · `scoring.py` (rubric 1) ·
`trace.py` (traces, secret guard) · `runner.py`. Traces go to `results/`
(git-ignored). Tests: `tests/experiments/` (offline, run in CI).
