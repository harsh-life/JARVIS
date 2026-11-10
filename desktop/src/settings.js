// Settings: a form over the settings commands (capabilities/settings.json).
// It never sees a credential: sign-in and sign-out run entirely in Rust, and
// what comes back is the non-secret view below.
const tauri = window.__TAURI__;
const invoke = (cmd, args) => tauri.core.invoke(cmd, args);
const $ = (id) => document.getElementById(id);

const STATUS = {
  not_configured: "No server set", signed_out: "Signed out", signing_in: "Signing in — finish in your browser…",
  connected: "Connected", offline: "Offline (the character shows its baseline)",
  revoked: "Signed out — this desktop was revoked or its key is no longer accepted", error: "Problem",
};

function render(view) {
  $("server_url").value = view.server_url || "";
  $("web_url").value = view.web_url || "";
  $("web_url").placeholder = view.web_url_effective ? `${view.web_url_effective} (default)` : "(set a server first)";
  const scale = $("scale");
  scale.replaceChildren(...view.scales.map((n) => {
    const o = document.createElement("option");
    o.value = String(n);
    o.textContent = `${n}× (${80 * n} × ${96 * n} px)`;
    o.selected = n === view.scale;
    return o;
  }));
  for (const key of ["visible", "click_through", "backdrop", "start_at_login"]) $(key).checked = !!view[key];
  $("reduced_motion").checked = view.reduced_motion === "always";
  showConnection(view.connection);
  const p = view.platform;
  $("platform").replaceChildren(...[
    ["Display session", p.session], ["Stays on top", p.always_on_top],
    ["Remembers position", p.remembers_position ? "yes" : "no"], ["Click-through", p.click_through],
    ["Version", view.version],
  ].flatMap(([k, v]) => {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = String(v);
    return [dt, dd];
  }));
  $("notes").replaceChildren(...p.notes.map((n) => {
    const li = document.createElement("li");
    li.textContent = n;
    return li;
  }));
}

function showConnection(c) {
  const state = (c && c.state) || "not_configured";
  const status = $("status");
  status.className = `status ${state}`;
  status.textContent = state === "error" && c.detail ? `Problem: ${c.detail}` : STATUS[state] || state;
  const signedIn = state === "connected" || state === "offline";
  $("sign_in").disabled = signedIn || state === "signing_in" || state === "not_configured";
  $("sign_out").disabled = !signedIn;
  $("server_url").disabled = signedIn;
  $("save_server").disabled = signedIn;
}

async function call(cmd, args, errorEl) {
  if (errorEl) errorEl.textContent = "";
  try {
    const result = await invoke(cmd, args);
    return result;
  } catch (err) {
    if (errorEl) errorEl.textContent = String(err);
    return null;
  }
}

async function patch(p, errorEl) {
  const view = await call("settings_set", { patch: p }, errorEl);
  if (view) render(view);
}

$("save_server").addEventListener("click", () => patch({ server_url: $("server_url").value }, $("account_error")));
$("save_web").addEventListener("click", () => patch({ web_url: $("web_url").value.trim() || null }, $("web_error")));
$("sign_in").addEventListener("click", async () => {
  showConnection({ state: "signing_in" });
  const view = await call("sign_in", {}, $("account_error"));
  if (view) render(view);
  else render(await call("settings_get", {}));
});
$("sign_out").addEventListener("click", async () => {
  const result = await call("sign_out", {}, $("account_error"));
  if (result) {
    render(result.settings);
    if (!result.server_confirmed) {
      $("account_error").textContent = "Signed out here. The server could not be reached, so this desktop is still "
        + "listed on your account: revoke it from your phone's device list.";
    }
  }
});
$("scale").addEventListener("change", (e) => patch({ scale: Number(e.target.value) }));
$("reduced_motion").addEventListener("change", (e) => patch({ reduced_motion: e.target.checked ? "always" : "system" }));
for (const key of ["visible", "click_through", "backdrop", "start_at_login"]) {
  $(key).addEventListener("change", (e) => patch({ [key]: e.target.checked }));
}

tauri.event.listen("connection", (e) => showConnection(e.payload));
call("settings_get", {}).then((view) => view && render(view));
