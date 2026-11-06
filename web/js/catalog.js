// Orchestration backends and specialist categories, as the operator offers
// them. A listed backend is a description; whether anything can run it is
// shown separately and decided by server code, not by the listing.
import { api } from "./api.js";
import { h, clear, stateTag, meta, errorBox, money } from "./dom.js";

let cached = null;
export async function loadCatalog(force = false) {
  if (!cached || force) cached = await api("GET", "/intelligence/catalog");
  return cached;
}
export function resetCatalog() { cached = null; }

const KIND = {
  planner: "Planner-only — returns a structured plan; JARVIS validates and dispatches it",
  agentic_runtime: "Agentic runtime — coordinates delegated work; in this phase it may only propose",
  workflow_engine: "Deterministic workflow — fixed hand-authored templates; no model decides",
};

function unavailable(root, err) {
  clear(root).append(h("div", { class: "panel" }, errorBox(err),
    err.code === "dependency_unavailable"
      ? h("p", { class: "muted" }, "The operator has not enabled the Workbench (workbench.enabled in config.yaml). Central intelligence still works.")
      : null));
}

export async function renderOrchestration(root) {
  clear(root).append(h("p", { class: "muted" }, "Loading…"));
  let cat;
  try { cat = await loadCatalog(); } catch (err) { unavailable(root, err); return; }
  const cards = cat.orchestration_backends.map((b) => h("div", { class: "card" },
    h("h3", {}, b.display_name),
    h("div", {}, stateTag(b.state), b.integration !== b.state ? stateTag(b.integration) : null,
      stateTag("not_active_in_production")),
    h("p", { class: "small" }, KIND[b.kind] || b.kind),
    meta([
      ["Backend id", b.backend_id], ["Type", b.kind], ["Adapter", b.adapter], ["Runtime", b.runtime],
      ["Needs a model", b.requires_model ? (b.allowed_models ? `yes — ${b.allowed_models.join(", ")}` : "yes — any catalogue model") : "no"],
      ["Endpoint", b.endpoint_configured ? "operator-configured (not shown)" : "none"],
      ["Delegation", b.delegation_mode], ["State model", b.state_model],
      ["Parallel", b.supports_parallel ? "supported" : "sequential only"],
      ["Max timeout", `${b.max_timeout_seconds}s`], ["Max fan-out", String(b.max_fan_out)],
      ["Budget ceiling", money(b.max_budget)], ["Config version", b.config_version],
    ]),
    b.capabilities.length ? h("p", { class: "small" }, "Capabilities: " + b.capabilities.join("; ")) : null,
    b.limitations.length ? h("p", { class: "small muted" }, "Limitations: " + b.limitations.join("; ")) : null,
  ));
  clear(root).append(
    h("div", { class: "panel" }, h("h2", {}, "Orchestration intelligence"),
      h("p", { class: "muted" },
        "Backends the operator has registered. You select one inside an experiment profile. None is active in production in this phase, and none is shown as connected because it is listed: “Not integrated” means no adapter exists yet."),
      h("p", { class: "small muted" },
        "Whatever the backend, JARVIS keeps identity, task scope, context permissions, budget, timeouts, cancellation, authorization and confirmation. No backend receives tools, credentials or memory.")),
    cards.length ? h("div", { class: "grid" }, cards) : h("p", { class: "muted" }, "No backends are offered."),
    h("div", { class: "card" }, h("h3", {}, "Models in the catalogue"),
      h("p", { class: "small muted" }, "Experiment-only entries cannot become your production model."),
      ...cat.models.map((m) => h("div", {},
        h("strong", {}, m.catalog_id), ` — ${m.provider} / ${m.model} `,
        m.production_selectable ? stateTag("production_selectable") : stateTag("experimental_only"),
        stateTag(m.experiment_state),
        h("span", { class: "small muted" }, m.pricing ? ` in ${money(m.pricing.input_per_1k_tokens)} / out ${money(m.pricing.output_per_1k_tokens)} per 1k` : " local, free")))),
  );
}

export async function renderSpecialists(root) {
  clear(root).append(h("p", { class: "muted" }, "Loading…"));
  let cat;
  try { cat = await loadCatalog(); } catch (err) { unavailable(root, err); return; }
  clear(root).append(
    h("div", { class: "panel" }, h("h2", {}, "Specialist assignments"),
      h("p", { class: "muted" },
        "Specialists give bounded reasoning with structured output. They see only the evidence a work package names, cannot read or write your memory, cannot call tools and cannot spawn other specialists. Assign them a model inside an experiment profile."),
      stateTag("experimental_only"), stateTag("not_active_in_production")),
    h("div", { class: "grid" }, cat.specializations.map((s) => h("div", { class: "card" },
      h("h3", {}, s.display_name),
      h("p", { class: "small" }, s.description),
      meta([["Id", s.id], ["Output schema", s.output_schema],
        ["Prompt profiles", s.prompt_profiles.map((p) => `${p.id} (v${p.versions.join(", v")})`).join("; ")]])))),
  );
}
