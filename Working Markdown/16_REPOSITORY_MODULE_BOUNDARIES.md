# 16_REPOSITORY_MODULE_BOUNDARIES.md
## Hypermind Track B — Repository & Module Boundaries

**Package:** subsystem doc 16 of 17 · **Depth:** standard · **Status:** implementation contract
**Authority:** subordinate to `00_CANONICAL_PRD` (§45 repo structure). Encodes, as import/dependency rules, the boundaries that `03`/`04`/`07`/`09`/`10`/`12` define semantically — so heavy AI-assisted (Claude Code) development can't silently erode them.
**Consumed by:** every implementer; `17` (a boundary-lint test).

**Why this exists:** with a lot of code generated fast, the failure mode isn't a wrong algorithm — it's `server/agent` quietly importing `secrets/store` and reading a raw key "for convenience," or `android/` ending up with a server secret. Semantic rules in other docs become real only if the module graph enforces them. This doc turns boundaries into **who-may-import-whom**.

**Label legend:** `[LOCKED]` · `[IMPL]` · `[FUTURE]` · `[OPEN — OWNER]`.

---

## 0. The rule this document exists to enforce

> **A module may only depend on what its boundary permits; dependencies point inward toward deterministic control, never outward into secrets/other-users/other-scopes.** Boundaries defined semantically elsewhere are enforced here as dependency direction.

---

## 1. Repository layout (from PRD §45)

```
hypermind-track-b/
  server/{gateway,auth,graph,agent,models,modeltools,tools,capabilities,
          fs,net,memory,vault,scheduler,secrets,intelligence,voice,
          dashboard,security,config}
  android/{auth,voice,capture,shizuku,permissions,action}
  shared/schemas/          # canonical data contracts (01) — both sides import
  docs/  tests/
```

`[LOCKED]` `shared/schemas/` is the *only* thing both `server/` and `android/` import. Otherwise server and phone are separate dependency worlds (§4).

---

## 2. Layering & dependency direction (`[LOCKED]`)

Dependencies point **inward** toward deterministic control and schemas; never outward toward secrets, other users, or presentation:

```
presentation (dashboard, reports)
        │ may import ▼
application (agent, modeltools, tools, memory, vault, scheduler, voice)
        │ may import ▼
control & policy (gateway, auth, graph, capabilities, security, net, fs)
        │ may import ▼
foundation (config, secrets*, shared/schemas)     (*secrets: special, §3)
```
`[LOCKED]` A lower layer never imports an upper layer (no `secrets` importing `agent`, no `graph` importing `dashboard`). Cyclic dependencies are forbidden.

---

## 3. The secrets boundary (the most important rule, ties `12`)

`[LOCKED]`
- **`server/secrets` exposes only the handle-based interface + the resolution-at-boundary function** — it does **not** export a "give me the raw value" function to general callers.
- **`server/agent` MUST NOT import `server/secrets`' resolution directly** — the agent references handles; the deterministic boundary (in `models`/`tools` adapters, invoked by the runtime) resolves them. An `agent` module importing raw-secret access is a boundary violation (SECRET-002, INV-6).
- **`server/tools` and `server/models` may resolve *their own declared* `secret_ref` at the call boundary**, never another scope's, never the master key (`12` §2/§4).
- **`android/*` MUST NOT contain or import any server secret** — the phone holds only its own device credential in secure storage (`03`/PHONE-004); no server API key ever lives in `android/` or the APK (SECRET-004).

```mermaid
flowchart LR
    AGENT["server/agent"] -->|handle only| RESOLVE["boundary resolver (in models/tools)"]
    RESOLVE -->|scoped get| SECRETS["server/secrets (SecretStore)"]
    AGENT -.FORBIDDEN.->|raw get| SECRETS
    ANDROID["android/*"] -.FORBIDDEN.-> SECRETS
```

---

## 4. Server vs phone boundary (`[LOCKED]`)

- `android/` and `server/` share **only** `shared/schemas/`.
- `android/` never imports server-side logic (authz, memory, secrets, tools) — it's a client: it authenticates (`03`), captures/acts on the device (`08`), and calls the API (`02`). Authorization lives server-side (PHONE-003).
- `server/` never depends on `android/`.

---

## 5. Cross-module rules that encode semantic boundaries

`[LOCKED]`
| Rule | Encodes | Doc |
|---|---|---|
| `graph`/`capabilities` (the authz engine) is imported *by* resource modules; it does not import them | authorization is a gate everything passes through, not a peer | `04` |
| `agent` cannot import `capabilities` to *grant* itself anything — it can only request via the runtime → authz path | agent proposes, doesn't authorize | `05`/`04` |
| the **Judge/validation** module does not import any offensive/Skill methodology (n/a to Track B, but the analogue:) the agent-runtime's authorization path is not importable by tools to self-approve | no self-authorization | `04`/`07` |
| `tools` modules cannot import each other's private scope/config — each tool sees only its own `ToolConfiguration` | tool isolation, blast-radius | `07`/`14` |
| `research`/dataset store (if present) exposes **write-only** to the live pipeline; no live module imports a read path | research firewall | `10`/PRD §10-analogue |
| `fs`/`net` boundary enforcers are imported by `tools` (which must pass through them); a tool cannot import a "raw filesystem"/"raw socket" helper that bypasses them | non-bypassable fs/net boundaries | `09`/`10` |
| `dashboard` is read-only over gated results; it cannot import mutation paths or `secrets` resolution | observability ≠ administration; no secret display | `28`/`12` |

---

## 6. Enforcement (keep AI-assisted dev honest)

`[REC]` Enforce the dependency rules mechanically, not by hope:
- a **module-boundary lint / import-linter** config that encodes the allowed dependency graph (§2–§5); a forbidden import fails CI.
- `[REC]` code-ownership markers on the security-critical modules (`auth`, `graph`, `capabilities`, `secrets`, `fs`, `net`) so changes there get human review (ties the ownership model — these are HUMAN-OWNED boundaries).
- `[LOCKED]` a boundary violation (e.g. `agent`→raw-`secrets`, or a server key in `android/`) is a **CI failure**, not a review nicety — because under heavy code generation, "the reviewer will catch it" is not a reliable control.

---

## 7. Open items

| ID | Question | Status |
|---|---|---|
| OD-REPO-1 | single repo (with server/android/shared) vs split repos | `[IMPL]`; PRD §45 rec single; the server/android/shared split-line is locked either way |
| OD-REPO-2 | import-linter tool choice | `[IMPL]`; mechanical-enforcement-in-CI locked |

---

## 8. Acceptance hooks (for `17`)

- **REPO-T1** `server/agent` cannot import raw-secret resolution; a build/lint fails if it does (SECRET-002/INV-6). *(release-blocking)*
- **REPO-T2** no server secret exists in `android/` or the APK (SECRET-004). *(release-blocking)*
- **REPO-T3** `android/` and `server/` share only `shared/schemas/`.
- **REPO-T4** no module imports an upper layer; no dependency cycles (§2).
- **REPO-T5** a tool cannot import a raw fs/socket helper that bypasses `fs`/`net` boundaries (§5).
- **REPO-T6** `dashboard` cannot import secret resolution or mutation paths (§5).
- **REPO-T7** the dependency rules are enforced in CI, and a deliberate violation fails the build (§6).

---

*End of 16_REPOSITORY_MODULE_BOUNDARIES. Continues to 17 (final).*
