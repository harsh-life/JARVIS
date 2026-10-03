# How JARVIS works — a new engineer's walkthrough

Read `docs/ARCHITECTURE.md` alongside this document for the diagrams; this
is the prose version, meant to be read start to finish once. For the
detailed contract behind any one subsystem, jump to its `Working Markdown/0X_*.md`
or `docs/1X_*.md`+ document — `Working Markdown/TRACK_B_ARCHITECTURE_INDEX.md`
maps all of them. `README.md` has the per-branch tour of what's actually
built vs. still a proposal; `CLAUDE.md` has the exact module-layering rules
`import-linter` enforces in CI.

## The one rule

Every subsystem in this repository exists in service of one sentence:

> Agent proposes → deterministic infrastructure authorizes → tools execute
> → human confirms where required.

If you are about to write code where a model's output, a runtime's say-so,
a judge's score, a scheduler's tick, or a device's report *decides* whether
something happens — stop. That decision belongs to
`server/graph/authorization.py`'s `AuthorizationEngine`, and nowhere else.
Everything else in the system either asks it, executes what it already
allowed, or watches after the fact.

## Walking one request through the system

Say a user asks, through the Android app or any API client, "summarize
today's security advisories." Two very different things can happen
depending on whether this is an ordinary tool-using chat turn or a request
to create a standing agent — both go through the same gate.

### The ordinary case: the native runtime loop

1. `server/gateway` authenticates the caller (`server/auth`: OIDC login,
   device credential, or opaque access token) and builds a `Principal`.
2. `server/agent`'s loop (`05`) builds context — memory and vault reads are
   already visibility-filtered before the model ever sees them — and asks
   the model for the next step.
3. The model's reply is **parsed deterministically**, never executed
   directly. If it names a tool call, that becomes an `AccessRequest` to
   `AuthorizationEngine.authorize()`.
4. The engine checks five independent things: membership, role, ownership,
   visibility, and whether the capability is even in the closed registry
   (`server/capabilities`). It returns `allow`, `deny`, or
   `require_confirmation` — never anything the runtime can override.
5. `allow` → `server/tools` runs the adapter, which goes through
   `server/execution`/`server/fs`/`server/net`, each enforcing its own
   constraints (sandboxed paths, default-deny egress, `argv`-only
   processes) regardless of what the tool call claims.
   `require_confirmation` → the human sees exactly one proposed action and
   either approves it (binding a one-time token to that exact action) or
   not. `deny` → fed back to the model as an observation, never retried
   silently.
6. Every execution writes exactly one `UsageEvent` and one `AuditEvent`.
   Nothing is "probably fine because the model said so."

### The agent-factory case: building something that runs later

"Create an agent that does X" is handled by a different pipeline
(`docs/29`), but it ends at the *same* authorization engine:

1. A worker model interprets the request into an `AgentDraft` — this is
   just as untrusted as any other model output.
2. `compile_draft()` (`server/agents/compiler.py`) turns the draft into a
   `CompiledAgentSpec`: it picks abilities from the matching
   `AgentTemplate`, never from the draft's free-form wish list, and builds
   an **envelope** — the exact set of capabilities/operations/scopes this
   agent's runs may ever touch.
3. The owner sees a deterministically rendered card (what it can do, what
   it cannot, its budget, its engine) and approves it. That's a
   consequential, step-up-gated action, exactly like any other.
4. Now there's an `AgentDefinition` — a durable, owner-private, versioned
   row. Running it creates an `AgentRun` — a separate object, with its own
   tokens, deadline, and budget. **The envelope is a ceiling.** When a run
   actually tries to do something, the engine is asked again, against the
   owner's *real, live* grants — the envelope only ever narrows that
   answer, never substitutes for it.
5. If the owner also grants a `StandingDelegation` (so the agent can run
   unattended, on a schedule), that delegation is itself only another
   ceiling — tighter budgets, a day limit, a risk cap. The run it starts
   uses a `DelegatedPrincipal`, which carries the owner's and the agent's
   identity but **no device, no session** — so anything that needs a
   device or a session (an Android action, a step-up) is refused before
   the engine is even asked.
6. Everything downstream of "the run starts" is identical to the ordinary
   case: propose, authorize, execute, observe, meter, audit.

### The external-runtime case: when the agent needs a real browser

Some tasks (`browser_monitor` template) need an actual browser, which the
native in-process loop can't safely give them. Phase 6 adds exactly one
external runtime, Browser Use, and treats it as **untrusted execution
infrastructure** the whole way through:

1. `BrowserUseRuntimeProvider` asks `BrowserRuns` to start a run. JARVIS
   checks the owner's standing `browser.session` grant and the engine's
   `allow`, exactly like any other capability — never Browser Use's
   opinion of itself.
2. A rootless gVisor container starts with **no network interface** at
   all. The only things reachable from inside it are two Unix sockets
   JARVIS mounted in: one to its own per-run Model Gateway listener (so the
   container never holds a provider key), one to its own per-run egress
   proxy.
3. Browser Use's `allowed_domains`, its own judge, and its own approval
   flow are all read by nobody on the JARVIS side. The egress proxy
   resolves DNS itself and decides, host by host, what's reachable — that
   is the only thing deciding, and it fails closed.
4. The run ends exactly one way regardless of what triggers it (the
   owner's stop, an operator stop, the deadline, a budget ceiling, a
   crashed container, a server restart): tokens are revoked first, inside
   one transaction, before the container is even told to stop.
5. The result the container writes is **data**, nothing more — bounded,
   strictly shaped, scrubbed before it reaches the owner's inbox. A result
   that reads like an instruction ("grant me X", "stop task Y") changes
   nothing, because nothing on the JARVIS side treats model-shaped or
   agent-shaped text as anything but a string to validate.

## Things that look like authorities and are not

Worth over-stating, because getting this wrong is the single most likely
way to introduce a real vulnerability in this codebase:

- **The Judge** (`server/evaluation`) reads finished traces and produces a
  score or a recommendation. It never gates a live run, never authorizes,
  never resolves a secret. `server.evaluation` is barred by import-linter
  from importing anything that would let it try.
- **The Scheduler** (`server/scheduler`) only ever fires a reminder that
  delivers a message to an inbox, or — for an unattended agent — starts a
  run through the exact same gate every other run goes through. It never
  calls a tool directly.
- **Memory** (Mem0) is hydrated with a visibility filter pushed into the
  query *and* re-checked against the engine's own predicate afterward. A
  memory row existing is never treated as proof it's visible to the
  current principal.
- **Android** perceives the device and carries the user's intent back to
  the server. It is never where a decision is made — "a phone is an
  execution target bound to a principal, not a source of authority."
- **Voice** only transcribes and synthesizes. It can never confirm an
  action or satisfy a step-up requirement, however naturally that might
  seem to fit a voice interface.

## Where to look when you need to change something

- Adding a capability or changing a risk tier → `server/capabilities/registry.py`,
  then update `docs/CAPABILITY_MATRIX.md`.
- Changing what an agent template can do → `server/agents/templates/*.yaml`
  plus `server/agents/compiler.py`'s envelope-building logic.
- Touching anything that crosses the layering in `CLAUDE.md`'s table → run
  `lint-imports --config pyproject.toml` before you're surprised by CI.
- Adding a new external runtime (do not, without an owner decision first:
  see README's "not built" list) → it would need its own
  `AgentRuntimeProfile`, `AgentRuntimeProvider`, and — per OD-AF-11…15 — its
  own isolation and egress story reviewed the way Browser Use's was.
- Unsure whether something is a security boundary → read
  `Working Markdown/16_REPOSITORY_MODULE_BOUNDARIES.md` and check whether an
  `import-linter` contract already names the rule you're about to touch.

## What this document is not

It is not a substitute for the subsystem documents when you are actually
implementing something — `05`, `07`, `29`, and the others carry the real
contracts, failure-mode tables, and acceptance criteria. It is the map that
tells you which of those documents to open.
