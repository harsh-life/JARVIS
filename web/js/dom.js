// Tiny DOM helpers. Text is only ever added as text nodes — never parsed as markup —
// so nothing the server or a user typed can become markup.
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  for (const child of children.flat()) {
    if (child === undefined || child === null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

const STATE_LABEL = {
  available: ["Available", "tag-ok"],
  configured: ["Configured", "tag-ok"],
  connection_required: ["Connection required", "tag-warn"],
  unavailable: ["Unavailable", "tag-bad"],
  experimental_only: ["Experimental only", "tag-info"],
  not_integrated: ["Not integrated", "tag-muted"],
  not_active_in_production: ["Not active in production", "tag-muted"],
  validation_failed: ["Validation failed", "tag-bad"],
  integrated: ["Harness adapter", "tag-info"],
  scripted: ["Scripted test double only", "tag-muted"],
  active_in_production: ["Active in production", "tag-ok"],
  disabled: ["Disabled", "tag-muted"],
  production_selectable: ["Production-selectable", "tag-ok"],
};

export function stateTag(state) {
  const [label, cls] = STATE_LABEL[state] || [state, "tag-muted"];
  return h("span", { class: `tag ${cls}` }, label);
}

export function meta(pairs) {
  const dl = h("dl", { class: "meta" });
  for (const [k, v] of pairs) {
    if (v === undefined) continue;
    dl.append(h("dt", {}, k), h("dd", {}, v === null ? "—" : v));
  }
  return dl;
}

export function issuesList(issues) {
  if (!issues || !issues.length) return h("p", { class: "small tag-ok" }, "No validation issues.");
  return h("ul", { class: "issues" },
    issues.map((i) => h("li", { class: i.severity || "error" }, `${i.path}: ${i.message} (${i.code})`)));
}

export function errorBox(err) {
  const lines = [err.message];
  if (err.details && Array.isArray(err.details.issues)) {
    return h("div", { class: "error", role: "alert" }, err.message, issuesList(err.details.issues));
  }
  if (err.code === "dependency_unavailable" && err.details.dependency) lines.push(`(${err.details.dependency})`);
  return h("p", { class: "error", role: "alert" }, lines.join(" "));
}

export function money(v) { return v === null || v === undefined ? "—" : Number(v).toFixed(4); }
