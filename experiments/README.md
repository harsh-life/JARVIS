# experiments/ — the isolated intelligence experiment harness

Not production: see `docs/31_INTELLIGENCE_EXPERIMENTS.md`. Nothing in `server/`
imports this package, and it reaches no production state. Synthetic tasks only.

```bash
python -m experiments make-snapshot --document profile.json --config config.yaml --out snap.json
python -m experiments run central      --snapshot snap.json   # E0/E1 (by the snapshot's Central prompt profile)
python -m experiments run orchestrated --snapshot snap.json   # E2/E3 (by the snapshot's backend)
python -m experiments run synthesis    --snapshot snap.json   # E4
# add --live --providers providers.yaml and HYPERMIND_EXPERIMENTS_LIVE=1 for real calls
python -m experiments compare results/<run> [results/<run> …] --out results/compare-<label>
                                                              # A/B/C report; re-finalizes, never edits a run
```

Layout: `contracts.py` (WorkPackage, ResultBundle, typed bodies, Plan) ·
`accounting.py` (per-call metering, reserve-before-dispatch) · `providers.py`
(provider map, scripted providers) · `prompts.py` + `prompt_profiles/` ·
`central.py` (E0/E1, synthesis) · `specialists.py` · `orchestration/` (ports,
validator, backends, dispatcher) · `tasks/` (suites) · `scoring.py` (rubric 1) ·
`trace.py` (traces, secret guard) · `runner.py`. Phase 0.1 (docs/31 §9):
`actions.py` (action outcomes + the trusted `ActionLedger`) · `finalizer.py`
(the response-truthfulness boundary: status is rendered from the ledger, never
from model text) · `claims.py` (the lexical screen, defence in depth) ·
`axes.py` (A quality / B protocol / C truthfulness) · `compare.py` ·
`tasks/action_truthfulness_v1.yaml` (14 adversarial prompts × 11 scripted
execution states). Phase 0.2 (docs/31 §10): `intent.py` (intent contract,
grounding) · `recognizers.py` (deterministic, model-assisted, hybrid) ·
`explanations.py` (reason codes, evidence-anchored facts) · `routing.py`
(answer / action_status / clarify; `admit` is the only path into the ledger) ·
`intent_eval.py` (`python -m experiments intent-eval [--live|--replay DIR] --out DIR`) ·
`tasks/intent_v1.yaml` (67, dev) and `tasks/intent_holdout_v1.yaml` (36, held out). Phase 0.3 (docs/31 §11):
`suspicion.py` (routing-only side-effect signal) · `conditions.py` · `referents.py` · `rules_v2.py`
(v2 rules and hybrid; `recognizers.py` stays the Phase 0.2 version) · `intent_eval_v3.py`
(`python -m experiments phase03-eval [--live|--replay DIR] --out DIR`) · `annotation_tools.py` +
`annotation/phase03_packet/` (blind packet for a second annotator) · `tasks/intent_holdout_v2.yaml` (140, held out). Traces go to `results/`
(git-ignored). Tests: `tests/experiments/` (offline, run in CI).
