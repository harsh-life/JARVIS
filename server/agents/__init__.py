"""agents — the Agent Factory (docs/29).

`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]` (docs/29, OD-AF-1). Built behind
`agents.enabled: false` as an implemented recommendation; nothing activates
for users until an operator enables it after the owner signs §32.

What this package is, and is not:

* It turns a worker's **AgentDraft** (semantic intent, data only) into an
  immutable **CompiledAgentSpec** through deterministic code — the registries
  (`registry/`), the ability table (`abilities.py`), the selector
  (`selector.py`) and the compiler (`compiler.py`, the spec's only writer) —
  and persists owner-private definitions (`service.py`).
* It is **never an authority**. A spec stores a *ceiling* (docs/29 §10.1);
  every operation an agent will ever attempt is still decided by the owner's
  live grants, activation, the mode ceiling and the authorization engine
  (`04`), exactly as for any task. This package grants nothing, confirms
  nothing, resolves no secret (import contract AF-C1) and decides no access:
  the composition root (`server/composition/agents.py`) asks the engine.

Phase 1 of docs/29 §31 only: no agent runs, no inbox, no notebook, no gateway
tokens, no scheduler integration, no delegation, no external provider.
"""
