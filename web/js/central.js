// Central intelligence — the user's PRODUCTION model. This is M2's existing
// per-user model selection (/config/model, /secrets), not a second mechanism.
import { api } from "./api.js";
import { h, clear, stateTag, meta, errorBox, money } from "./dom.js";

function selectionState(sel, entry) {
  if (sel.source === "server_default") return "configured";
  if (sel.key_usable === false) return "unavailable";
  if (entry && entry.requires_user_key && !sel.key_ref) return "connection_required";
  return "configured";
}

export async function renderCentral(root) {
  clear(root).append(h("p", { class: "muted" }, "Loading…"));
  let catalog, selection;
  try {
    [catalog, selection] = await Promise.all([api("GET", "/config/model/catalog"), api("GET", "/config/model")]);
  } catch (err) {
    clear(root).append(errorBox(err));
    return;
  }
  const entries = catalog.items;
  const current = entries.find((e) => e.id === selection.catalog_id);
  const status = h("div");
  const state = selectionState(selection, current);

  const summary = h("div", { class: "card" },
    h("h3", {}, "Current production assignment"),
    h("div", {}, stateTag("active_in_production"), stateTag(state)),
    meta([
      ["Source", selection.source === "server_default" ? "Server default (operator's key)" : "Your selection"],
      ["Catalogue entry", selection.catalog_id],
      ["Provider", selection.provider],
      ["Model", selection.model],
      ["Your key", selection.key_ref ? (selection.key_usable ? "stored (write-only)" : "revoked — select again") : null],
    ]),
    h("p", { class: "small muted" },
      "This is the model your own tasks use today. Experiment profiles (other tabs) never change it."),
  );

  const choice = h("select", { id: "central-entry" },
    entries.map((e) => h("option", { value: e.id, selected: e.id === selection.catalog_id },
      `${e.id} — ${e.provider} / ${e.model}${e.local ? " (local)" : ""}`)));
  const keyInput = h("input", { type: "password", autocomplete: "off", spellcheck: "false", placeholder: "paste a new API key" });
  const reuse = h("input", { type: "checkbox", checked: Boolean(selection.key_ref && selection.key_usable) });
  const reuseLabel = h("label", { class: "inline" }, reuse, "Use my currently stored key");
  const details = h("div");

  function showEntry() {
    const e = entries.find((x) => x.id === choice.value);
    clear(details).append(meta([
      ["Provider", e.provider], ["Model", e.model], ["Local", e.local ? "yes" : "no"],
      ["Price / 1k in", e.pricing ? money(e.pricing.input_per_1k_tokens) : "free (local)"],
      ["Price / 1k out", e.pricing ? money(e.pricing.output_per_1k_tokens) : "free (local)"],
      ["Needs your key", e.requires_user_key ? "yes" : "no"],
    ]));
    keyInput.disabled = !e.requires_user_key;
    reuseLabel.hidden = !(e.requires_user_key && selection.key_ref && selection.key_usable);
  }
  choice.addEventListener("change", showEntry);
  showEntry();

  async function save(ev) {
    ev.preventDefault();
    clear(status);
    const e = entries.find((x) => x.id === choice.value);
    try {
      let keyRef = null;
      if (e.requires_user_key) {
        if (keyInput.value.trim()) {
          const stored = await api("POST", "/secrets", { value: keyInput.value.trim() });
          keyRef = stored.key_ref;
          keyInput.value = "";
        } else if (reuse.checked && selection.key_ref) {
          keyRef = selection.key_ref;
        } else {
          status.append(h("p", { class: "error" }, "This model needs your own API key (or use the server default)."));
          return;
        }
      }
      await api("PUT", "/config/model", { catalog_id: e.id, key_ref: keyRef });
      await renderCentral(root);
    } catch (err) {
      status.append(errorBox(err));
    }
  }

  async function reset() {
    clear(status);
    try { await api("DELETE", "/config/model"); await renderCentral(root); }
    catch (err) { status.append(errorBox(err)); }
  }

  const form = h("form", { class: "card", onsubmit: save },
    h("h3", {}, "Change production model"),
    h("p", { class: "small muted" },
      "Only operator-approved catalogue entries are offered. You never choose an endpoint; a key you paste is stored write-only and only its handle comes back."),
    h("div", { class: "row" }, h("label", { class: "grow" }, "Catalogue entry", choice)),
    details,
    h("div", { class: "row" }, h("label", { class: "grow" }, "API key (paid models only)", keyInput), reuseLabel),
    h("div", { class: "row" },
      h("button", { type: "submit" }, "Save selection"),
      h("button", { type: "button", class: "secondary", onclick: reset }, "Return to server default"),
      h("button", { type: "button", class: "secondary", disabled: true,
        title: "This server has no user-facing provider probe yet" }, "Test connection"),
    ),
    h("p", { class: "small muted" },
      "Test connection is unavailable: the server offers no user-facing provider probe yet (only the operator console can probe providers). A failing key shows up as a model_unavailable task error."),
    status,
  );

  clear(root).append(
    h("div", { class: "panel" }, h("h2", {}, "Central intelligence"),
      h("p", { class: "muted" },
        "Central understands intent, keeps the user-facing perspective, weighs specialist contributions and writes the final answer. In production today it is your single selected model.")),
    summary, form,
  );
}
