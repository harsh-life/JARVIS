"""The isolated intelligence experiment harness (docs/31, Phase 0).

Not production. Nothing under `server/` imports this package (import-linter
contract "Production never imports the experiment harness"), it is never
mounted as an API, and it reaches no production state:

* no SecretStore, no database, no memory or vault, no tools, no execution
  primitive, no authorization engine, no Agent Factory, no gateway — each is
  a forbidden import (see `pyproject.toml`);
* it runs synthetic tasks only (`experiments/tasks/`), and every model call is
  individually metered against a budget reserved *before* the call;
* configuration arrives as an immutable `ExperimentSnapshot` exported by the
  Workbench (or built from a local profile document for offline tests), and
  every result records that snapshot's configuration hash.

What it may reuse: the `server.models` adapters (`ModelProvider`), the shared
schemas, and `server.config` (to build a snapshot with the server's own
catalogue validation). The E0 baseline is the production runtime's prompt and
parser rules, held byte-for-byte to `server.agent` by a test rather than
imported (importing would load the runtime package into the harness).

Isolated is not offline: with `--live`, calls go to the inference providers
named in an explicitly supplied provider map (local Ollama, or a remote API
with a key read from an environment variable the operator names). Without
`--live`, only scripted providers run, which is what CI does.
"""
