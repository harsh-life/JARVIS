# 06_MODEL_PROVIDER_LLM_TOOL.md
## Hypermind Track B — Model Provider & LLM-as-Tool

**Package:** subsystem doc 06 of 17 · **Depth:** compact (PRD MODEL-001..005 + MODELTOOL-001..004 already cover the substance; this doc adds only the interface/isolation contract) · **Status:** implementation contract
**Authority:** subordinate to `00_CANONICAL_PRD`. Realizes MODEL-001..005, MODELTOOL-001..004, uses `ModelConfiguration`/`ToolConfiguration` (`01` §9), secrets by handle (`12`), invoked by the runtime (`05`), metered by `13`.

**Label legend:** `[LOCKED]` · `[IMPL]` · `[FUTURE]` · `[OPEN — OWNER]`.

---

## 1. ModelProvider interface (MODEL-001)

`[LOCKED]` The runtime speaks **one normalized interface**, never a vendor SDK directly. Adapters implement it per provider.

```
interface ModelProvider:
    invoke(messages, generation_policy, timeout) -> ModelResult   # normalized in/out
    health() -> ok | unavailable
```
- **Providers** (`01` enum): `ollama`, `openai`, `anthropic`, `gemini`, `deepseek`, `groq`, `openai_compatible`, `custom` (user-hosted endpoint). Adding one is an adapter + config, **not** a runtime change (MODEL-002, P4/P5).
- **Normalization** `[LOCKED]`: adapters translate provider-specific request/response into the normalized `messages`→`ModelResult` shape so the runtime and the rest of the system are provider-agnostic. Provider quirks never leak upward.
- **Secrets** `[LOCKED]`: an adapter resolves its API key via `ModelConfiguration.secret_ref` → SecretStore (`12`) at call time. The key is **never** inline in config, never in the model messages, never logged (SECRET-004). Keyless local (ollama) has `secret_ref = null`.
- **Timeout/retry/cost** `[LOCKED]`: each call carries a timeout (`ModelConfiguration.timeout_seconds`); retry policy is bounded (`06` retry ⊆ `05` bounds); every call emits a `UsageEvent` with tokens/estimated_cost (`13`, USAGE-001).

---

## 2. Local/open-weight-first (MODEL-001, P4)

`[LOCKED]` The default and recommended primary is a **local model via Ollama** (Qwen-class), consistent with Track B's local-first posture — no per-worker cloud dependency, no per-call cost, data stays local. Cloud providers are available but are a *choice*, not a requirement, and are never a hidden dependency. `[IMPL]` which local model; `[REC]` a small capable instruct model, upsized only on evaluated need.

---

## 3. LLM-as-a-Tool (MODELTOOL-001..004)

`[LOCKED]` Other LLMs may be registered as **tools** the primary agent can invoke (e.g. a "Gemini" tool for spreadsheet reasoning, a "DeepSeek" tool for code). This is **configuration, not hard-coded routing** — the user/server chooses which model-tools exist.

- **A model-tool is a `ToolConfiguration` (`01` §9.3) whose underlying tool is a model invoker**, carrying a `ModelConfiguration`. It flows through the *same* tool machinery as any other tool (`07`): registered contract, required capability, authorization via `04`, bounded execution, metering.
- **No MCP-per-provider** `[LOCKED]` (MODELTOOL-003): the internal model-tool abstraction is native; MCP is only an optional adapter for *external* MCP servers (`07`/TOOL-004), never the mechanism for calling models.
- **Discovery** `[LOCKED]`: the agent learns which model-tools are available from its resolved `AgentConfiguration.model_tools` (`01` §4.1) — it cannot invoke a model-tool that isn't configured+enabled.
- **Input/output normalization**: a model-tool's I/O conforms to its `ToolContract` (`01` §10) so the agent treats it like any tool (input schema in, output schema out).
- **Nesting bound** `[LOCKED]` (ties `05` §3): a model-tool invocation counts against `max_model_tool_nesting_depth` — an agent → model-tool → (that model proposing another model-tool) chain is capped, preventing unbounded model-calling-model recursion.

---

## 4. Provider isolation (MODELTOOL-004, blast-radius)

`[LOCKED]`
- Each provider adapter is isolated: a failing/misbehaving adapter (timeout, malformed response, error) fails **that call only** (FAIL-005), never crashes the runtime; the runtime handles it per `05` §6.
- One provider's secret is scoped to that provider's `secret_ref`; a compromised adapter cannot read another provider's secret (secrets are handle-scoped, `12`).
- A model-tool's output is **untrusted content** flowing back into the agent's context — it is data, never instructions (prompt-injection defense; ties the TB-B-style rule): the runtime treats a model-tool result like any external observation, and `04`/`05` still gate any *action* the agent proposes as a result of it.

---

## 5. Cost/latency controls (ties `13`)
- Per-call timeout + bounded retry (§1).
- Every model/model-tool call → `UsageEvent{kind:model_call, provider, model, tokens, estimated_cost}` (USAGE-001).
- Per-task/per-user budget enforced against the usage ledger (USAGE-002); a paid-provider call that would breach budget is refused → explicit failure (RATE-001), not silently made.

---

## 6. Open items

| ID | Question | Status |
|---|---|---|
| OD-MT-1 | Default local model choice | `[IMPL]`, `[REC]` small Qwen-class |
| OD-MT-2 | Whether any task justifies a cloud primary by default | `[OPEN — OWNER]`; default local |
| OD-RT-1 (from 05) | nesting depth | shared; finite cap locked |

---

## 7. Acceptance hooks (for `17`)

- **MP-T1** the primary model is swappable via config with no runtime code change (MODEL-001).
- **MP-T2** an API key never appears in config-in-repo, in model messages, in logs, or in a UsageEvent (SECRET-004).
- **MP-T3** an LLM can be invoked as a tool and flows through the same authorization + metering as any tool (MODELTOOL-001).
- **MP-T4** the agent cannot invoke a model-tool that isn't in its resolved config.
- **MP-T5** a failing provider fails only its call, not the runtime (isolation).
- **MP-T6** a model-tool result is treated as untrusted data; an action proposed from it is still gated by `04`.
- **MP-T7** model-tool nesting cannot exceed the cap (shared with RT-T7).
- **MP-T8** a budget-breaching paid call is refused explicitly, not made silently.

---

*End of 06_MODEL_PROVIDER_LLM_TOOL. Continues to 07.*
