// The companion window: the shared ActivityPolicy drives the sprite renderer.
//
// This page holds no credential and makes no network request: the Rust core
// sends it validated activity reports as events, and it may call only the
// companion commands (capabilities/companion.json). Reports and expressions
// stay in memory and are never stored or logged.
import { ActivityPolicy, parseActivityReport, parseSettings } from "./vendor/activity-policy.js";
import PRESENTATION from "./vendor/presentation.js";
import ATLAS from "./sprites/jarvis-atlas.js";
import { CanvasRenderer, SpriteAnimator, validateAtlas } from "./renderer.js";

const tauri = window.__TAURI__;
const invoke = (cmd, args) => tauri.core.invoke(cmd, args);
const listen = (event, fn) => tauri.event.listen(event, (e) => fn(e.payload));
const now = () => performance.now();

const SETTINGS = parseSettings(PRESENTATION);
const canvas = document.getElementById("character");
const badge = document.getElementById("badge");
const menuButton = document.getElementById("menu");

let policy = new ActivityPolicy(SETTINGS, now());
let animator = null;
let renderer = null;
let prefs = { scale: 2, reduced_motion: "system", backdrop: false };
let visible = true;
let smoke = false;
let frameTimer = null;
let policyTimer = null;

const systemReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const reducedMotion = () => prefs.reduced_motion === "always" || systemReducedMotion.matches;

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = src;
  });
}

// A static, single-frame atlas: what is drawn when the real one is unusable.
function fallbackAtlas() {
  const frame = { x: 0, y: 0 };
  return {
    frame_size: [80, 96], image_size: [80, 96], frames: { f0: frame },
    animations: { static_default: { frames: ["f0"], durations_ms: [1000], loop: false },
      idle_default: { frames: ["f0"], durations_ms: [1000], loop: true } },
  };
}

async function setUpRenderer() {
  try {
    const image = await loadImage("sprites/jarvis-atlas.png");
    const check = validateAtlas(ATLAS, [image.naturalWidth, image.naturalHeight]);
    if (!check.ok) throw new Error(check.errors[0]);
    return { kind: "sprite", image, atlas: ATLAS };
  } catch {
    const image = await loadImage("sprites/jarvis-fallback.png");
    return { kind: "fallback", image, atlas: fallbackAtlas() };
  }
}

// ── drawing: one timer, only while visible, only when a frame changes ────

function draw() {
  clearTimeout(frameTimer);
  frameTimer = null;
  if (!renderer || !visible) return;
  const t = now();
  renderer.draw(animator.frameAt(t));
  const next = animator.nextFrameAt(t);
  if (next !== null) frameTimer = setTimeout(() => requestAnimationFrame(draw), Math.max(16, next - t));
}

function applyPolicy() {
  if (animator) animator.setTarget(policy.shown, policy.intensity, now());
  if (smoke) invoke("companion_status", { shown: policy.shown, intensity: policy.intensity }).catch(() => {});
  draw();
  schedulePolicy();
}

function schedulePolicy() {
  clearTimeout(policyTimer);
  const at = policy.nextChangeAt(now());
  if (at !== null) {
    policyTimer = setTimeout(() => {
      policy.tick(now());
      applyPolicy();
    }, Math.max(50, at - now()));
  }
}

function applyPrefs(p) {
  prefs = { ...prefs, ...p };
  if (renderer) {
    renderer.backdrop = !!prefs.backdrop;
    renderer.resize(prefs.scale, window.devicePixelRatio);
  }
  if (animator) animator.setReducedMotion(reducedMotion(), now());
  document.body.classList.toggle("click-through", !!prefs.click_through);
  draw();
}

const CONNECTION_BADGES = {
  not_configured: "Not set up — right-click for the menu",
  signed_out: "Signed out",
  signing_in: "Signing in…",
  offline: "Offline",
  revoked: "Signed out by the server",
  error: "Problem — open Settings",
};

function showConnection(c) {
  const text = CONNECTION_BADGES[c && c.state];
  badge.hidden = !text;
  badge.title = text || "";
  badge.setAttribute("aria-label", text || "");
}

// ── interaction: click = local activity; double click = open JARVIS ─────

let clickTimer = null;
let press = null;

canvas.addEventListener("pointerdown", (e) => {
  if (e.button === 0) press = { x: e.screenX, y: e.screenY };
});
canvas.addEventListener("pointermove", (e) => {
  if (press && Math.hypot(e.screenX - press.x, e.screenY - press.y) > 4) {
    press = null;
    invoke("start_drag").catch(() => {});
  }
});
canvas.addEventListener("pointerup", () => {
  if (!press) return;
  press = null;
  clearTimeout(clickTimer);
  clickTimer = setTimeout(() => {
    policy.onLocalActivity(now());
    if (animator) animator.poke(now());
    applyPolicy();
  }, 260);
});
canvas.addEventListener("dblclick", () => {
  clearTimeout(clickTimer);
  invoke("open_jarvis").catch(() => {});
});
const openMenu = (e) => {
  e.preventDefault();
  invoke("show_menu").catch(() => {});
};
canvas.addEventListener("contextmenu", openMenu);
menuButton.addEventListener("click", openMenu);
systemReducedMotion.addEventListener("change", () => applyPrefs({}));

// ── start ────────────────────────────────────────────────────────────────

async function start() {
  const r = await setUpRenderer();
  renderer = new CanvasRenderer(canvas, r.image, r.atlas);
  animator = new SpriteAnimator(r.atlas, now(), { reducedMotion: false });

  await listen("activity", (text) => {
    // Validated again here, with the shared parser; an invalid report changes nothing.
    policy.onReport(parseActivityReport(text), now());
    applyPolicy();
  });
  await listen("local-activity", () => {
    policy.onLocalActivity(now());
    applyPolicy();
  });
  await listen("signed-out", () => {
    policy = new ActivityPolicy(SETTINGS, now());   // forget the last report at once
    applyPolicy();
  });
  await listen("prefs", applyPrefs);
  await listen("connection", showConnection);
  await listen("visibility", (v) => {
    visible = !!v;
    animator.setVisible(visible, now());
    if (visible) {
      policy.onLocalActivity(now());
      applyPolicy();
    } else {
      clearTimeout(frameTimer);
    }
  });

  const init = await invoke("companion_ready", { renderer: r.kind });
  smoke = !!init.smoke;
  showConnection(init.connection);
  applyPrefs(init);
  applyPolicy();
}

start().catch(() => {
  // Without the IPC bridge (opened outside the app) the page stays blank.
});
