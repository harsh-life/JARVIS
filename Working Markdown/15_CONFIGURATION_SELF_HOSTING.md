# 15_CONFIGURATION_SELF_HOSTING.md
## Hypermind Track B — Configuration & Self-Hosting

**Package:** subsystem doc 15 of 17 · **Depth:** deep · **Status:** implementation contract
**Authority:** subordinate to `00_CANONICAL_PRD`. Realizes HOST-001/002, P5 (self-hostable), OSS-001 (open/proprietary boundary), SECRET-004 (no secrets in repo). Uses the SecretStore (`12`) for all credential material.
**Consumed by:** anyone deploying Track B; `16` (module boundaries constrain config wiring); `17` (self-host acceptance test).

**Label legend:** `[LOCKED]` · `[IMPL]` · `[FUTURE]` · `[OPEN — OWNER]`.

---

## 0. The rule this document exists to enforce

> **Another developer can clone, configure, and run Track B on their own hardware with their own credentials — needing none of the original owner's keys, tunnel, data, or secrets — and can change the primary model, model-tools, tools, server, credentials, or disable the IntelligenceProvider by configuration alone, without editing source.** (HOST-001/002, P5)

Documented bootstrap steps (clone, install, init) are expected and fine; what's forbidden is a *source edit* to do a normal configuration change (HOST-002).

---

## 1. Config-only vs code boundary (HOST-002)

`[LOCKED]` **Changeable by config alone (no source edit):** the primary agent model + provider; which LLMs are available as model-tools; which generic tools are enabled; the server host/port/base-URL/tunnel; API credentials (as `secret_ref`/env, never literals); enabling/disabling the IntelligenceProvider; voice providers; rate/budget limits; capability defaults.
`[LOCKED]` **Requires code (legitimately):** adding a *new kind* of tool/provider adapter that doesn't exist yet (that's development, not configuration) — but adding/removing/selecting among *existing* ones is config.

---

## 2. Configuration surface (the schema)

`[LOCKED shape; [IMPL] format (YAML/TOML/env)]` — a single, documented config surface:

```
server:      { host, port, base_url, tunnel: { provider, config_ref } }
agent:       { provider, model, generation_policy }          # primary agent (06)
models_as_tools:
             [ { id, provider, model, secret_ref, enabled } ] # LLM-as-tool (06/14)
tools:       [ { tool_id, enabled, config, secret_ref, overrides } ]  # (07)
memory:      { mem0: { collection: "hypermind_memories", path, embedder }, }  # (11)
vault:       { collection: "hypermind_vault", git_backed, path }             # distinct! (11)
intelligence:{ enabled: false, provider?, config? }          # default false (25)
voice:       { stt, diarization, speaker_id, tts }           # replaceable providers (27)
security:    { auth: { oidc: { client_id, issuer } },
               rate_limits, budgets, capability_defaults }   # (03/13/16)
secrets:     { store: "encrypted_local", kek_source }        # (12) — the KEK SOURCE, never the KEK
```

`[LOCKED]`
- **Every credential is a `secret_ref` or an env-var name — never a literal value in this file** (SECRET-004). The config says *where* to get a secret, never *what* it is.
- **`mem0.collection` and `vault.collection` are distinct** (VAULT-003) — the config makes the separation explicit and a same-value misconfig is a validation error at load.
- **`intelligence.enabled` defaults `false`** (INTEL-003) — Track B runs fully without it.

---

## 3. Bootstrap / deployment flow (HOST-001)

`[LOCKED as the required steps; [IMPL] the scripts]`:
```
1. clone the repo
2. install prerequisites (Python, Docker, Ollama, OPA, tunnel client)   [documented]
3. create config from a template (config.example → config)              [no secrets in template]
4. set up secrets: initialize the SecretStore, provide the KEK out-of-band, add own API keys (12)
5. initialize storage: DB, Mem0 collection, Vault collection (distinct)  [migration/init step]
6. pull the chosen local model(s) via Ollama (or configure a cloud provider)
7. start the server (FastAPI)
8. set up the tunnel (Cloudflare or equivalent) — tunnel credential stays server-side (12)
9. pair a phone: OIDC login → device registration (03)
10. health check: /api/v1/health + a smoke assessment
```
`[LOCKED]` Steps 2/3/5/6/8 are documented setup, not source edits — consistent with HOST-002. No step requires the original owner's secrets/data (HOST-001).

---

## 4. What belongs where (OSS-001 — open vs private boundary)

`[LOCKED]`
| Belongs in… | Content |
|---|---|
| **source control (git)** | all code; config **templates** (`config.example`); tool/skill/manifest *definitions*; the Knowledge Vault's *shared curated* content (git-backed, VAULT-001) |
| **`.env` / local config (git-ignored)** | the active config; env-var *names* resolved to secrets; base URL; provider selections |
| **SecretStore (local, encrypted)** | all secret *values* — API keys, OAuth tokens, device credentials, tunnel credential (`12`) |
| **local data directories (git-ignored)** | Mem0 store, per-user files, dataset, logs, audit/usage ledgers |
| **outside git entirely** | the KEK/master key (out-of-band, `12`); any real user data; private/proprietary Intelligence Provider content |

`[LOCKED]` **No part of the open repo requires the owner's API keys, tunnel credentials, personal data, user memory, private files, or production secrets** (HOST-001). A fresh clone runs on the cloner's own credentials.

---

## 5. Open-source / proprietary boundary (OSS-001)

`[LOCKED]` Infrastructure (the runtime, adapters, tool framework, memory/vault machinery) may be open; **private** stays private: user data, credentials, curated proprietary intelligence, and any future private Intelligence Provider (`00` §25). A self-hoster's data/secrets are theirs; a future hosted-Hypermind's proprietary intelligence can remain closed behind the same `IntelligenceProvider` socket.

---

## 6. Config validation (fail-closed)
`[LOCKED]` At load, config is validated: distinct mem0/vault collections (else error); no literal secrets present (else error — a literal-looking key in config is a load failure, SECRET-004); required OIDC identity fields present; `intelligence.enabled` explicit. A malformed/secret-bearing config fails to start (fail-closed), with an explicit message — never a silent partial start.

---

## 7. Open items

| ID | Question | Status |
|---|---|---|
| OD-CFG-1 | config format (YAML/TOML/env-layered) | `[IMPL]`; shape + no-literal-secrets locked |
| OD-CFG-2 | tunnel provider abstraction (Cloudflare-only vs pluggable) | `[IMPL]`; server-side-credential rule locked |
| OD-CFG-3 | one-command bootstrap script vs documented steps | `[IMPL]`; no-source-edit-for-config rule locked |

---

## 8. Acceptance hooks (for `17`)

- **CFG-T1** a fresh clone runs on the cloner's own credentials with no owner secrets (HOST-001). *(the self-host test)*
- **CFG-T2** changing the primary model / a model-tool / an enabled tool / the server / credentials / disabling intelligence is config-only, no source edit (HOST-002).
- **CFG-T3** no secret literal exists anywhere in the repo or config templates (SECRET-004).
- **CFG-T4** mem0 and vault collections are distinct; a same-value config fails to start (VAULT-003).
- **CFG-T5** `intelligence.enabled: false` and Track B functions fully (INTEL-003).
- **CFG-T6** a config bearing a literal secret fails validation at load (fail-closed).

---

*End of 15_CONFIGURATION_SELF_HOSTING. Continues to 16.*
