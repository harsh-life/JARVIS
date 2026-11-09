// Entry point: connect with an access token (held in memory only), then show
// the Workbench's tabs. No tier, capability or authorization logic lives in
// this client — the server decides, and the page renders what it was told.
import { api, setToken, ApiError } from "./api.js";
import { h, clear } from "./dom.js";
import { renderCentral } from "./central.js";
import { renderOrchestration, renderSpecialists, resetCatalog } from "./catalog.js";
import { renderProfiles } from "./profiles.js";

const RENDER = {
  central: renderCentral,
  orchestration: renderOrchestration,
  specialists: renderSpecialists,
  profiles: renderProfiles,
};

function show(tab) {
  for (const btn of document.querySelectorAll("#tabs button")) btn.classList.toggle("active", btn.dataset.tab === tab);
  for (const name of Object.keys(RENDER)) document.getElementById(`tab-${name}`).hidden = name !== tab;
  RENDER[tab](document.getElementById(`tab-${tab}`));
}

async function connect(ev) {
  ev.preventDefault();
  const input = document.getElementById("token");
  const error = document.getElementById("signin-error");
  error.textContent = "";
  setToken(input.value.trim());
  input.value = "";
  try {
    const me = await api("GET", "/me");
    resetCatalog();
    clear(document.getElementById("identity")).append(
      h("span", {}, `Connected as ${me.display_name || me.user_id}`), " ",
      h("button", { type: "button", class: "secondary", onclick: () => window.location.reload() }, "Disconnect"));
    document.getElementById("signin").hidden = true;
    document.getElementById("tabs").hidden = false;
    show("central");
  } catch (err) {
    setToken(null);
    error.textContent = err instanceof ApiError && err.status === 401
      ? "That token was refused (expired, revoked or not an access token)."
      : `Could not connect: ${err.message}`;
  }
}

document.getElementById("signin-form").addEventListener("submit", connect);
for (const btn of document.querySelectorAll("#tabs button")) btn.addEventListener("click", () => show(btn.dataset.tab));
