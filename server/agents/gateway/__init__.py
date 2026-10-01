"""The Agent Gateway (docs/29 §11–§13) — Phase 3, in-process.

`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]` (docs/29). The single choke point
through which every agent run reaches models and tools:

* `tokens.py` — run tokens and request nonces: pure rules (§11.3);
* `core.py` — the request check every request passes: token, binding, run,
  definition, expiry, revocation, freshness, nonce, replay/idempotency;
* `model_gateway.py` — the Model Gateway's rules: which model an alias may
  reach, under the owner's current policy, and the agent's month (§12);
* `model_routing.py` — model-as-tool routing: a role becomes a permitted
  configured model tool, which is then an ordinary `model.invoke` call.

Native runs use it in-process today (Phase 3). The network surface for
external runtimes — an OpenAI-compatible Model Gateway, an MCP Tool Gateway —
is Phase 6 and is not built. The gateway authorizes nothing: what a request it
admits asks is decided by the envelope gate, activation, the ceilings and the
one engine (04), as the run's present owner.
"""
