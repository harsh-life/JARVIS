// Experiment profiles: named, versioned intelligence configurations for the
// isolated experiment harness. Saving creates a new immutable version with a
// configuration hash; nothing here is active in production.
import { api } from "./api.js";
import { h, clear, stateTag, meta, errorBox, issuesList } from "./dom.js";
import { loadCatalog } from "./catalog.js";

let selectedId = null;

function num(input) { return input.value === "" ? undefined : Number(input.value); }
function opts(list, current) {
  return list.map(([value, label]) => h("option", { value, selected: value === current }, label));
}
function numberInput(value, attrs = {}) {
  return h("input", { type: "number", value: value ?? "", step: attrs.step || "any", min: attrs.min ?? 0, max: attrs.max });
}

function defaultDocument(cat) {
  const model = (cat.models.find((m) => m.local) || cat.models[0] || {}).catalog_id;
  const backend = cat.orchestration_backends.find((b) => b.integration === "integrated") || cat.orchestration_backends[0];
  return {
    central: { catalog_id: model, prompt_profile: "central.contract", prompt_version: "1" },
    orchestration: {
      backend_id: backend ? backend.backend_id : "",
      model_catalog_id: backend && backend.requires_model ? model : null,
      timeout_seconds: 30, max_fan_out: Math.min(3, backend ? backend.max_fan_out : 3), max_depth: 1, budget: 0,
    },
    specialists: [],
    execution: { mode: "sequential", per_call_timeout_seconds: 30, overall_timeout_seconds: 120, per_call_budget: 0, overall_budget: 0 },
    context_policy: { mode: "task_only", max_chars: 6000 },
    evaluation: { suite_id: "synthetic.v1", rubric_version: "1", repetitions: 1 },
  };
}

function buildEditor(cat, doc) {
  const modelOpts = cat.models.map((m) => [m.catalog_id, `${m.catalog_id} — ${m.provider}/${m.model}${m.local ? " (local)" : ""}`]);
  const f = {};

  // Central
  f.centralModel = h("select", {}, opts(modelOpts, doc.central.catalog_id));
  f.centralProfile = h("select", {}, opts(cat.central_prompt_profiles.map((p) => [p.id, p.id]), doc.central.prompt_profile));
  f.centralVersion = h("input", { value: doc.central.prompt_version });

  // Orchestration
  const backendOpts = cat.orchestration_backends.map((b) => [b.backend_id, `${b.display_name} (${b.kind}${b.integration === "not_integrated" ? ", not integrated" : ""})`]);
  f.backend = h("select", {}, opts(backendOpts, doc.orchestration.backend_id));
  f.orchModel = h("select", {}, [h("option", { value: "" }, "— none —"), ...opts(modelOpts, doc.orchestration.model_catalog_id || "")]);
  f.orchTimeout = numberInput(doc.orchestration.timeout_seconds);
  f.fanOut = numberInput(doc.orchestration.max_fan_out, { step: 1, min: 1 });
  f.orchBudget = numberInput(doc.orchestration.budget);
  const backendInfo = h("div");
  function syncBackend() {
    const b = cat.orchestration_backends.find((x) => x.backend_id === f.backend.value);
    f.orchModel.disabled = !(b && b.requires_model);
    if (f.orchModel.disabled) f.orchModel.value = "";
    clear(backendInfo).append(b ? h("div", {}, stateTag(b.state), b.integration !== b.state ? stateTag(b.integration) : null,
      h("span", { class: "small muted" }, ` delegation ${b.delegation_mode}, state ${b.state_model}, fan-out ≤ ${b.max_fan_out}, timeout ≤ ${b.max_timeout_seconds}s, ${b.supports_parallel ? "parallel ok" : "sequential only"}`)) : "");
  }
  f.backend.addEventListener("change", syncBackend);
  syncBackend();

  // Specialists — one row per offered specialization
  f.specialists = cat.specializations.map((s) => {
    const existing = doc.specialists.find((x) => x.specialization === s.id);
    const a = existing || { enabled: true, catalog_id: doc.central.catalog_id, prompt_profile: s.prompt_profiles[0].id,
      prompt_version: s.prompt_profiles[0].versions[0], context_policy: { mode: "evidence_subset", max_chars: 6000 },
      timeout_seconds: 30, max_output_tokens: 800, budget: 0 };
    const row = {
      spec: s,
      assigned: h("input", { type: "checkbox", checked: Boolean(existing) }),
      enabled: h("input", { type: "checkbox", checked: a.enabled }),
      model: h("select", {}, opts(modelOpts, a.catalog_id)),
      profile: h("select", {}, opts(s.prompt_profiles.map((p) => [p.id, p.id]), a.prompt_profile)),
      version: h("input", { value: a.prompt_version }),
      context: h("select", {}, opts([["evidence_subset", "Named evidence only"], ["none", "No context"]], a.context_policy.mode)),
      maxChars: numberInput(a.context_policy.max_chars, { step: 1 }),
      timeout: numberInput(a.timeout_seconds),
      tokens: numberInput(a.max_output_tokens, { step: 1, min: 16 }),
      budget: numberInput(a.budget),
    };
    row.el = h("fieldset", {},
      h("legend", {}, s.display_name),
      h("div", { class: "row" },
        h("label", { class: "inline" }, row.assigned, "Assign"),
        h("label", { class: "inline" }, row.enabled, "Enabled"),
        h("span", { class: "small muted" }, `output ${s.output_schema}`)),
      h("div", { class: "grid" },
        h("label", {}, "Model", row.model), h("label", {}, "Prompt profile", row.profile),
        h("label", {}, "Prompt version", row.version), h("label", {}, "Context policy", row.context),
        h("label", {}, "Context max chars", row.maxChars), h("label", {}, "Call timeout (s)", row.timeout),
        h("label", {}, "Max output tokens", row.tokens), h("label", {}, "Budget", row.budget)));
    return row;
  });

  // Execution, context, evaluation
  const ex = doc.execution;
  f.mode = h("select", {}, opts([["sequential", "Sequential"], ["parallel", "Parallel"]], ex.mode));
  f.perCallTimeout = numberInput(ex.per_call_timeout_seconds);
  f.overallTimeout = numberInput(ex.overall_timeout_seconds);
  f.perCallBudget = numberInput(ex.per_call_budget);
  f.overallBudget = numberInput(ex.overall_budget);
  f.ctxMode = h("select", {}, opts([["task_only", "Task context only"], ["evidence_subset", "Named evidence only"], ["none", "No context"]], doc.context_policy.mode));
  f.ctxChars = numberInput(doc.context_policy.max_chars, { step: 1 });
  f.suite = h("input", { value: doc.evaluation.suite_id });
  f.rubric = h("input", { value: doc.evaluation.rubric_version });
  f.reps = numberInput(doc.evaluation.repetitions, { step: 1, min: 1, max: 5 });

  const el = h("div", {},
    h("fieldset", {}, h("legend", {}, "Central intelligence (experiment)"),
      h("p", { class: "small muted" }, "Separate from your production model on the Central tab."),
      h("div", { class: "grid" }, h("label", {}, "Model", f.centralModel), h("label", {}, "Prompt profile", f.centralProfile),
        h("label", {}, "Prompt version", f.centralVersion), h("label", {}, "Context policy", f.ctxMode), h("label", {}, "Context max chars", f.ctxChars))),
    h("fieldset", {}, h("legend", {}, "Orchestration"), backendInfo,
      h("div", { class: "grid" }, h("label", {}, "Backend", f.backend), h("label", {}, "Backend model", f.orchModel),
        h("label", {}, "Timeout (s)", f.orchTimeout), h("label", {}, "Max fan-out", f.fanOut), h("label", {}, "Budget", f.orchBudget)),
      h("p", { class: "small muted" }, "Plan depth is fixed at 1: specialists never spawn specialists.")),
    h("fieldset", {}, h("legend", {}, "Specialists"), ...f.specialists.map((r) => r.el)),
    h("fieldset", {}, h("legend", {}, "Execution, budget and evaluation"),
      h("div", { class: "grid" }, h("label", {}, "Execution mode", f.mode),
        h("label", {}, "Per-call timeout (s)", f.perCallTimeout), h("label", {}, "Overall timeout (s)", f.overallTimeout),
        h("label", {}, "Per-call budget", f.perCallBudget), h("label", {}, "Overall budget", f.overallBudget),
        h("label", {}, "Evaluation suite", f.suite), h("label", {}, "Rubric version", f.rubric), h("label", {}, "Repetitions", f.reps))),
  );

  function collect() {
    return {
      central: { catalog_id: f.centralModel.value, prompt_profile: f.centralProfile.value, prompt_version: f.centralVersion.value },
      orchestration: { backend_id: f.backend.value, model_catalog_id: f.orchModel.value || null,
        timeout_seconds: num(f.orchTimeout), max_fan_out: num(f.fanOut), max_depth: 1, budget: num(f.orchBudget) },
      specialists: f.specialists.filter((r) => r.assigned.checked).map((r) => ({
        specialization: r.spec.id, enabled: r.enabled.checked, catalog_id: r.model.value,
        prompt_profile: r.profile.value, prompt_version: r.version.value, output_schema: r.spec.output_schema,
        context_policy: { mode: r.context.value, max_chars: num(r.maxChars) },
        timeout_seconds: num(r.timeout), max_output_tokens: num(r.tokens), budget: num(r.budget),
      })),
      execution: { mode: f.mode.value, per_call_timeout_seconds: num(f.perCallTimeout), overall_timeout_seconds: num(f.overallTimeout),
        per_call_budget: num(f.perCallBudget), overall_budget: num(f.overallBudget) },
      context_policy: { mode: f.ctxMode.value, max_chars: num(f.ctxChars) },
      evaluation: { suite_id: f.suite.value, rubric_version: f.rubric.value, repetitions: num(f.reps) },
    };
  }
  return { el, collect };
}

function statusBlock(status, validation, hash) {
  return h("div", { class: "card" },
    h("h3", {}, "Status"),
    h("div", {}, stateTag("not_active_in_production"), stateTag(status.overall),
      status.runnable_in_harness ? h("span", { class: "tag tag-info" }, "Runnable in the experiment harness")
        : h("span", { class: "tag tag-muted" }, "Not runnable in the harness")),
    meta([["Configuration hash", hash ? h("span", { class: "hash" }, hash) : null]]),
    h("div", { class: "small" }, "Central: ", stateTag(status.central), " Orchestration: ", stateTag(status.orchestration),
      ...Object.entries(status.specialists).flatMap(([k, v]) => [` ${k}: `, stateTag(v)])),
    validation ? issuesList(validation.issues) : null,
  );
}

async function renderEditor(pane, cat, profile, onChange) {
  const doc = profile ? profile.latest.document : defaultDocument(cat);
  const editor = buildEditor(cat, doc);
  const out = h("div");
  const nameInput = h("input", { value: profile ? profile.name : "", maxlength: 80, required: true });

  async function validate() {
    clear(out);
    try {
      const r = await api("POST", "/intelligence/validate", { document: editor.collect() });
      out.append(statusBlock(r.status, r, r.config_hash));
    } catch (err) { out.append(errorBox(err)); }
  }
  async function save() {
    clear(out);
    try {
      let saved;
      if (profile) {
        saved = await api("POST", `/intelligence/profiles/${profile.profile_id}/versions`,
          { base_version: profile.current_version, document: editor.collect() });
      } else {
        saved = await api("POST", "/intelligence/profiles", { name: nameInput.value, document: editor.collect() });
      }
      selectedId = saved.profile_id;
      await onChange();
    } catch (err) { out.append(errorBox(err)); }
  }
  async function exportSnapshot(version) {
    clear(out);
    try {
      const snap = await api("GET", `/intelligence/profiles/${profile.profile_id}/versions/${version}/snapshot`);
      const text = JSON.stringify(snap, null, 2);
      const url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
      out.append(h("div", { class: "card" }, h("h3", {}, `Snapshot of v${version}`),
        h("p", { class: "small muted" }, "Immutable. Give this file to the experiment harness (python -m experiments run --snapshot …)."),
        h("a", { href: url, download: `profile-${profile.profile_id}-v${version}.json` }, "Download snapshot JSON"),
        h("pre", {}, text)));
    } catch (err) { out.append(errorBox(err)); }
  }
  async function remove() {
    clear(out);
    if (!window.confirm(`Delete “${profile.name}” and all its versions?`)) return;
    try { await api("DELETE", `/intelligence/profiles/${profile.profile_id}`); selectedId = null; await onChange(); }
    catch (err) { out.append(errorBox(err)); }
  }

  const header = profile
    ? h("div", { class: "card" }, h("h3", {}, profile.name),
        meta([["Current version", `v${profile.current_version}`], ["Updated", profile.updated_at]]),
        h("p", { class: "small muted" }, "Saving creates a new version; earlier versions are never changed."),
        h("div", {}, h("strong", { class: "small" }, "Versions: "),
          ...profile.versions.map((v) => h("div", { class: "small" },
            `v${v.version} · `, h("span", { class: "hash" }, v.config_hash.slice(0, 16) + "…"), ` · ${v.created_at} `,
            h("button", { type: "button", class: "secondary", onclick: () => exportSnapshot(v.version) }, "Export snapshot")))))
    : h("div", { class: "card" }, h("h3", {}, "New profile"), h("label", {}, "Name", nameInput));

  clear(pane).append(
    header,
    profile ? statusBlock(profile.latest.status, profile.latest.validation, profile.latest.config_hash) : null,
    editor.el,
    h("div", { class: "row" },
      h("button", { type: "button", class: "secondary", onclick: validate }, "Validate"),
      h("button", { type: "button", onclick: save }, profile ? `Save as v${profile.current_version + 1}` : "Create profile"),
      profile ? h("button", { type: "button", class: "danger", onclick: remove }, "Delete profile") : null),
    out,
  );
}

export async function renderProfiles(root) {
  clear(root).append(h("p", { class: "muted" }, "Loading…"));
  let cat, list;
  try {
    cat = await loadCatalog();
    list = await api("GET", "/intelligence/profiles");
  } catch (err) {
    clear(root).append(h("div", { class: "panel" }, errorBox(err)));
    return;
  }
  const pane = h("div");
  const side = h("div", { class: "list" },
    h("button", { type: "button", class: selectedId === null ? "active" : "", onclick: () => { selectedId = null; renderProfiles(root); } }, "+ New profile"),
    ...list.items.map((p) => h("button", { type: "button", class: p.profile_id === selectedId ? "active" : "",
      onclick: () => { selectedId = p.profile_id; renderProfiles(root); } }, `${p.name} · v${p.current_version}`)));
  clear(root).append(
    h("div", { class: "panel" }, h("h2", {}, "Experiment profiles"),
      h("p", { class: "muted" }, "Named, versioned configurations for the isolated experiment harness. Every run records the configuration hash of the snapshot it ran."),
      stateTag("experimental_only"), stateTag("not_active_in_production")),
    h("div", { class: "split" }, side, pane),
  );
  let profile = null;
  if (selectedId) {
    try { profile = await api("GET", `/intelligence/profiles/${selectedId}`); }
    catch (err) { selectedId = null; pane.append(errorBox(err)); return; }
  }
  await renderEditor(pane, cat, profile, () => renderProfiles(root));
}
