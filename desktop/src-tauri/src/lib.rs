//! JARVIS desktop companion — the native core (OD-DESK-1…3).
//!
//! The companion **presents**; it never authorizes. It shows JARVIS's
//! operational activity as the pixel-art character, using the server's
//! activity report and the shared presentation policy. It cannot submit,
//! cancel or confirm a task, approve anything, or execute anything — and the
//! server enforces that too: a desktop session reaches only its activity
//! report, its own logout and its own revocation (server/gateway/device_scope.py).
//!
//! Split of responsibilities:
//! * **Rust (here):** sign-in, the device key in the OS credential store,
//!   access tokens (memory only), every HTTPS request, polling and its
//!   lifecycle, the tray, the windows, OS lock/wake, opening the browser,
//!   local non-secret preferences.
//! * **Webview (`desktop/src`):** the shared ActivityPolicy, the sprite
//!   renderer, the settings form. It receives validated reports and
//!   non-secret settings only, and can call exactly the commands in
//!   `COMPANION_COMMANDS` / `SETTINGS_COMMANDS` (capabilities/*.json) —
//!   no HTTP, no filesystem, no shell, no credential.

pub mod account;
pub mod activity;
pub mod identity;
pub mod keystore;
pub mod logging;
pub mod loopback;
pub mod net;
pub mod platform;
pub mod prefs;

use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime};

use log::{info, warn};
use serde::{Deserialize, Serialize};
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::TrayIconBuilder;
use tauri::{
    AppHandle, Emitter, LogicalSize, Manager, PhysicalPosition, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
    WindowEvent,
};

use crate::account::{Account, AccountError, LOGIN_TIMEOUT};
use crate::keystore::OsStore;
use crate::net::ServerOrigin;
use crate::prefs::{Prefs, ReducedMotion, SCALES};

pub const COMPANION: &str = "companion";
pub const SETTINGS: &str = "settings";
/// The commands each window may call — mirrored by `build.rs` and the
/// capability files, and checked against both by the tests.
pub const COMPANION_COMMANDS: [&str; 5] =
    ["companion_ready", "companion_status", "open_jarvis", "show_menu", "start_drag"];
pub const SETTINGS_COMMANDS: [&str; 4] = ["settings_get", "settings_set", "sign_in", "sign_out"];

/// The sprite frame (desktop/src/sprites/jarvis-atlas.json `frame_size`).
pub const FRAME: (u32, u32) = (80, 96);

/// The poll interval: the shared presentation defaults, as on Android.
fn poll_interval() -> Duration {
    let doc: serde_json::Value =
        serde_json::from_str(include_str!("../../../assets/character/presentation.json")).expect("presentation.json");
    Duration::from_millis(doc["activity"]["poll_interval_ms"].as_u64().unwrap_or(15_000))
}

#[derive(Debug, Clone, PartialEq, Serialize)]
#[serde(tag = "state", content = "detail", rename_all = "snake_case")]
pub enum Connection {
    NotConfigured,
    SignedOut,
    SigningIn,
    Connected,
    Offline,
    Revoked,
    Error(String),
}

pub struct AppState {
    prefs: Mutex<Prefs>,
    prefs_path: PathBuf,
    account: Arc<Account>,
    connection: Mutex<Connection>,
    wake: tokio::sync::Notify,
    smoke: Option<Smoke>,
}

struct Smoke {
    path: PathBuf,
    result: Mutex<serde_json::Value>,
}

#[derive(Serialize)]
struct CompanionInit {
    scale: u32,
    reduced_motion: ReducedMotion,
    backdrop: bool,
    click_through: bool,
    connection: Connection,
    frame: (u32, u32),
    smoke: bool,
}

#[derive(Serialize)]
struct SettingsView {
    server_url: Option<String>,
    web_url: Option<String>,
    web_url_effective: Option<String>,
    scale: u32,
    scales: [u32; 3],
    reduced_motion: ReducedMotion,
    click_through: bool,
    start_at_login: bool,
    backdrop: bool,
    visible: bool,
    connection: Connection,
    platform: platform::Capabilities,
    version: &'static str,
}

#[derive(Deserialize, Default)]
#[serde(deny_unknown_fields)]
struct SettingsPatch {
    server_url: Option<String>,
    web_url: Option<Option<String>>,
    scale: Option<u32>,
    reduced_motion: Option<ReducedMotion>,
    click_through: Option<bool>,
    start_at_login: Option<bool>,
    backdrop: Option<bool>,
    visible: Option<bool>,
}

fn state(app: &AppHandle) -> tauri::State<'_, AppState> {
    app.state::<AppState>()
}

fn set_connection(app: &AppHandle, c: Connection) {
    let s = state(app);
    let changed = {
        let mut cur = s.connection.lock().unwrap();
        let changed = *cur != c;
        *cur = c.clone();
        changed
    };
    if changed {
        let _ = app.emit_to(COMPANION, "connection", &c);
        let _ = app.emit_to(SETTINGS, "connection", &c);
        refresh_menus(app);
    }
}

fn save_prefs(app: &AppHandle) {
    let s = state(app);
    let p = s.prefs.lock().unwrap().clone();
    if let Err(e) = p.save(&s.prefs_path) {
        warn!("could not save preferences: {e}");
    }
}

fn companion(app: &AppHandle) -> Option<WebviewWindow> {
    app.get_webview_window(COMPANION)
}

fn web_entry(prefs: &Prefs) -> Option<url::Url> {
    if let Some(u) = &prefs.web_url {
        return net::validate_web_url(u).ok();
    }
    let origin = ServerOrigin::parse(prefs.server_url.as_deref()?).ok()?;
    url::Url::parse(&format!("{}/ui/", origin.as_str())).ok()
}

// ── window behaviour ────────────────────────────────────────────────────

fn apply_window_prefs(app: &AppHandle) {
    let Some(w) = companion(app) else { return };
    let p = state(app).prefs.lock().unwrap().clone();
    let _ = w.set_size(LogicalSize::new((FRAME.0 * p.scale) as f64, (FRAME.1 * p.scale) as f64));
    let _ = w.set_ignore_cursor_events(p.click_through);
    let _ = w.set_always_on_top(true);
    let _ = app.emit_to(COMPANION, "prefs", serde_json::json!({
        "scale": p.scale, "reduced_motion": p.reduced_motion, "backdrop": p.backdrop, "click_through": p.click_through,
    }));
}

fn restore_position(app: &AppHandle, w: &WebviewWindow) {
    let caps = platform::capabilities();
    let saved = state(app).prefs.lock().unwrap().position;
    if caps.remembers_position {
        if let Some((x, y)) = saved {
            // Only onto a monitor that is still there.
            let on_screen = w.available_monitors().unwrap_or_default().iter().any(|m| {
                let (p, s) = (m.position(), m.size());
                x >= p.x && y >= p.y && x < p.x + s.width as i32 - 32 && y < p.y + s.height as i32 - 32
            });
            if on_screen {
                let _ = w.set_position(PhysicalPosition::new(x, y));
                return;
            }
        }
        // Default: bottom-right of the primary work area, clear of the edge.
        if let Ok(Some(m)) = w.primary_monitor() {
            let (p, s) = (m.position(), m.size());
            let size = w.outer_size().unwrap_or_default();
            let margin = (48.0 * m.scale_factor()) as i32;
            let _ = w.set_position(PhysicalPosition::new(
                p.x + s.width as i32 - size.width as i32 - margin,
                p.y + s.height as i32 - size.height as i32 - margin * 2,
            ));
        }
    }
}

fn set_visible(app: &AppHandle, visible: bool) {
    if let Some(w) = companion(app) {
        let _ = if visible { w.show() } else { w.hide() };
    }
    state(app).prefs.lock().unwrap().visible = visible;
    save_prefs(app);
    let _ = app.emit_to(COMPANION, "visibility", visible);
    state(app).wake.notify_one();
    refresh_menus(app);
}

fn set_click_through(app: &AppHandle, on: bool) {
    state(app).prefs.lock().unwrap().click_through = on;
    save_prefs(app);
    apply_window_prefs(app);
    refresh_menus(app);
}

fn open_settings(app: &AppHandle) {
    if let Some(w) = app.get_webview_window(SETTINGS) {
        let _ = w.show();
        let _ = w.set_focus();
        return;
    }
    let built = WebviewWindowBuilder::new(app, SETTINGS, WebviewUrl::App("settings.html".into()))
        .title("JARVIS Companion — Settings")
        .inner_size(520.0, 680.0)
        .min_inner_size(420.0, 480.0)
        .resizable(true)
        .build();
    if let Err(e) = built {
        warn!("could not open settings: {e}");
    }
}

fn open_jarvis_page(app: &AppHandle) {
    let target = web_entry(&state(app).prefs.lock().unwrap());
    match target {
        Some(url) => {
            if tauri_plugin_opener::open_url(url.as_str(), None::<&str>).is_err() {
                warn!("could not open the browser");
            }
        }
        None => open_settings(app),
    }
    // Opening JARVIS is local activity.
    let _ = app.emit_to(COMPANION, "local-activity", ());
}

// ── menus (tray and the in-window fallback) ─────────────────────────────

fn build_menu(app: &AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    let s = state(app);
    let p = s.prefs.lock().unwrap().clone();
    let c = s.connection.lock().unwrap().clone();
    let signed_in = matches!(c, Connection::Connected | Connection::Offline);
    let sizes = Submenu::with_items(
        app,
        "Size",
        true,
        &SCALES
            .iter()
            .map(|n| {
                CheckMenuItem::with_id(app, format!("scale:{n}"), format!("{n}×"), true, p.scale == *n, None::<&str>)
            })
            .collect::<tauri::Result<Vec<_>>>()?
            .iter()
            .map(|i| i as &dyn tauri::menu::IsMenuItem<tauri::Wry>)
            .collect::<Vec<_>>(),
    )?;
    let status = match &c {
        Connection::Connected => "Connected".to_string(),
        Connection::Offline => "Offline — showing the baseline".to_string(),
        Connection::SignedOut => "Signed out".to_string(),
        Connection::NotConfigured => "No server configured".to_string(),
        Connection::SigningIn => "Signing in…".to_string(),
        Connection::Revoked => "Signed out by the server".to_string(),
        Connection::Error(e) => format!("Problem: {e}"),
    };
    Menu::with_items(
        app,
        &[
            &MenuItem::with_id(app, "status", status, false, None::<&str>)?,
            &PredefinedMenuItem::separator(app)?,
            &MenuItem::with_id(app, "toggle", if p.visible { "Hide" } else { "Show" }, true, None::<&str>)?,
            &MenuItem::with_id(app, "open", "Open JARVIS", web_entry(&p).is_some(), None::<&str>)?,
            &MenuItem::with_id(
                app,
                if signed_in { "sign_out" } else { "sign_in" },
                if signed_in { "Sign out" } else { "Sign in…" },
                !matches!(c, Connection::SigningIn),
                None::<&str>,
            )?,
            &PredefinedMenuItem::separator(app)?,
            &CheckMenuItem::with_id(app, "click_through", "Click-through", true, p.click_through, None::<&str>)?,
            &sizes,
            &CheckMenuItem::with_id(
                app,
                "reduced_motion",
                "Reduced motion",
                true,
                p.reduced_motion == ReducedMotion::Always,
                None::<&str>,
            )?,
            &CheckMenuItem::with_id(app, "start_at_login", "Start at login", true, p.start_at_login, None::<&str>)?,
            &MenuItem::with_id(app, "settings", "Settings…", true, None::<&str>)?,
            &PredefinedMenuItem::separator(app)?,
            &MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?,
        ],
    )
}

fn refresh_menus(app: &AppHandle) {
    if let Some(tray) = app.tray_by_id("main") {
        if let Ok(menu) = build_menu(app) {
            let _ = tray.set_menu(Some(menu));
        }
    }
}

fn on_menu(app: &AppHandle, id: &str) {
    match id {
        "toggle" => {
            let visible = state(app).prefs.lock().unwrap().visible;
            set_visible(app, !visible);
        }
        "open" => open_jarvis_page(app),
        "sign_in" => {
            let app = app.clone();
            tauri::async_runtime::spawn(async move {
                let _ = do_sign_in(&app).await;
            });
        }
        "sign_out" => {
            let app = app.clone();
            tauri::async_runtime::spawn(async move {
                let _ = do_sign_out(&app).await;
            });
        }
        "click_through" => {
            let on = !state(app).prefs.lock().unwrap().click_through;
            set_click_through(app, on);
        }
        "reduced_motion" => {
            {
                let s = state(app);
                let mut p = s.prefs.lock().unwrap();
                p.reduced_motion = if p.reduced_motion == ReducedMotion::Always {
                    ReducedMotion::System
                } else {
                    ReducedMotion::Always
                };
            }
            save_prefs(app);
            apply_window_prefs(app);
            refresh_menus(app);
        }
        "start_at_login" => {
            let on = !state(app).prefs.lock().unwrap().start_at_login;
            set_autostart(app, on);
        }
        "settings" => open_settings(app),
        "quit" => app.exit(0),
        other if other.starts_with("scale:") => {
            if let Ok(n) = other[6..].parse::<u32>() {
                if SCALES.contains(&n) {
                    state(app).prefs.lock().unwrap().scale = n;
                    save_prefs(app);
                    apply_window_prefs(app);
                    refresh_menus(app);
                }
            }
        }
        _ => {}
    }
}

fn set_autostart(app: &AppHandle, on: bool) {
    use tauri_plugin_autostart::ManagerExt;
    let launcher = app.autolaunch();
    let result = if on { launcher.enable() } else { launcher.disable() };
    match result {
        Ok(()) => {
            state(app).prefs.lock().unwrap().start_at_login = on;
            save_prefs(app);
        }
        Err(e) => warn!("could not change start at login: {e}"),
    }
    refresh_menus(app);
}

// ── sign-in / sign-out ──────────────────────────────────────────────────

async fn do_sign_in(app: &AppHandle) -> Result<(), String> {
    let account = state(app).account.clone();
    if account.origin().await.is_none() {
        set_connection(app, Connection::NotConfigured);
        open_settings(app);
        return Err("Set your JARVIS server's address first.".into());
    }
    set_connection(app, Connection::SigningIn);
    let opened = account
        .sign_in(|url| tauri_plugin_opener::open_url(url.as_str(), None::<&str>).map_err(|_| ()), LOGIN_TIMEOUT)
        .await;
    match opened {
        Ok(()) => {
            set_connection(app, Connection::Connected);
            let _ = app.emit_to(COMPANION, "local-activity", ());
            state(app).wake.notify_one();
            Ok(())
        }
        Err(e) => {
            set_connection(app, Connection::Error(e.to_string()));
            Err(e.to_string())
        }
    }
}

async fn do_sign_out(app: &AppHandle) -> Result<bool, String> {
    let account = state(app).account.clone();
    let result = account.sign_out().await.map_err(|e| e.to_string());
    set_connection(
        app,
        if account.origin().await.is_some() { Connection::SignedOut } else { Connection::NotConfigured },
    );
    // A signed-out companion forgets the last report at once.
    let _ = app.emit_to(COMPANION, "signed-out", ());
    result
}

// ── the activity poller ─────────────────────────────────────────────────

async fn poller(app: AppHandle) {
    let interval = poll_interval();
    let tick = Duration::from_secs(5);
    let mut due = Instant::now();
    let mut failures: u32 = 0;
    let mut was_locked = false;
    let mut last_wall = SystemTime::now();
    loop {
        let s = state(&app);
        let visible = s.prefs.lock().unwrap().visible;
        let signed_in = s.account.signed_in().await;
        if !visible || !signed_in {
            // Hidden or signed out: no requests, no ticking — wait to be woken.
            let _ = tokio::time::timeout(Duration::from_secs(300), s.wake.notified()).await;
            due = Instant::now();
            last_wall = SystemTime::now();
            continue;
        }
        tokio::select! {
            _ = tokio::time::sleep(tick) => {}
            _ = s.wake.notified() => { due = Instant::now(); }
        }
        // Sleep detection: the wall clock jumped while this task was not running.
        let wall = SystemTime::now();
        let gap = wall.duration_since(last_wall).unwrap_or_default();
        last_wall = wall;
        let woke = gap > tick + Duration::from_secs(30);

        let locked =
            tauri::async_runtime::spawn_blocking(platform::screen_locked).await.ok().flatten().unwrap_or(false);
        if locked {
            was_locked = true;
            continue;
        }
        if was_locked || woke {
            was_locked = false;
            info!("session resumed; fetching a fresh report");
            let _ = app.emit_to(COMPANION, "local-activity", ());
            due = Instant::now();
        }
        if Instant::now() < due {
            continue;
        }
        match s.account.activity().await {
            Ok(report) => {
                failures = 0;
                let _ = app.emit_to(COMPANION, "activity", report.to_json());
                set_connection(&app, Connection::Connected);
                due = Instant::now() + interval;
            }
            Err(AccountError::Revoked) | Err(AccountError::NotSignedIn) => {
                set_connection(&app, Connection::Revoked);
                let _ = app.emit_to(COMPANION, "signed-out", ());
            }
            Err(e) => {
                failures = failures.saturating_add(1);
                warn!("activity request failed: {e}");
                set_connection(&app, Connection::Offline);
                let backoff = interval.saturating_mul(1 << failures.min(4)).min(Duration::from_secs(300));
                due = Instant::now() + backoff;
            }
        }
    }
}

// ── commands: the companion window ──────────────────────────────────────

fn require(window: &tauri::Window, label: &str) -> Result<(), String> {
    if window.label() == label {
        Ok(())
    } else {
        Err("not permitted from this window".into())
    }
}

#[tauri::command]
fn companion_ready(window: tauri::Window, app: AppHandle, renderer: String) -> Result<CompanionInit, String> {
    require(&window, COMPANION)?;
    let s = state(&app);
    let p = s.prefs.lock().unwrap().clone();
    let connection = s.connection.lock().unwrap().clone();
    if let Some(smoke) = &s.smoke {
        smoke.result.lock().unwrap()["renderer"] = serde_json::json!(renderer == "sprite");
        // A synthetic, valid report — the smoke test proves the whole
        // report → policy → renderer path runs in the packaged webview.
        let synthetic = r#"{"schema_version":1,"computed_at":"2026-11-10T12:00:00Z","window_s":1800,"scores":{"happy":9,"satisfied":7,"default":5,"sleepy":1,"surprised":1,"angry":1,"sad":1},"components":{"engagement":80,"productivity":90,"steadiness":100,"failure":0,"pressure":10,"surprise":0,"inactivity":0}}"#;
        let report = activity::ActivityReport::parse(synthetic.as_bytes()).expect("valid synthetic report");
        let app2 = app.clone();
        tauri::async_runtime::spawn(async move {
            tokio::time::sleep(Duration::from_millis(400)).await;
            let _ = app2.emit_to(COMPANION, "activity", report.to_json());
        });
    }
    Ok(CompanionInit {
        scale: p.scale,
        reduced_motion: p.reduced_motion,
        backdrop: p.backdrop,
        click_through: p.click_through,
        connection,
        frame: FRAME,
        smoke: s.smoke.is_some(),
    })
}

/// What the character currently shows — used only by the smoke test; never
/// stored or logged.
#[tauri::command]
fn companion_status(window: tauri::Window, app: AppHandle, shown: String, intensity: u8) -> Result<(), String> {
    require(&window, COMPANION)?;
    if let Some(smoke) = &state(&app).smoke {
        let mut r = smoke.result.lock().unwrap();
        r["shown"] = serde_json::json!(shown);
        r["intensity"] = serde_json::json!(intensity);
    }
    Ok(())
}

#[tauri::command]
fn open_jarvis(window: tauri::Window, app: AppHandle) -> Result<(), String> {
    require(&window, COMPANION)?;
    open_jarvis_page(&app);
    Ok(())
}

#[tauri::command]
fn show_menu(window: tauri::Window, app: AppHandle) -> Result<(), String> {
    require(&window, COMPANION)?;
    let menu = build_menu(&app).map_err(|e| e.to_string())?;
    window.popup_menu(&menu).map_err(|e| e.to_string())
}

#[tauri::command]
fn start_drag(window: tauri::Window) -> Result<(), String> {
    require(&window, COMPANION)?;
    window.start_dragging().map_err(|e| e.to_string())
}

// ── commands: the settings window ───────────────────────────────────────

fn settings_view(app: &AppHandle) -> SettingsView {
    let s = state(app);
    let p = s.prefs.lock().unwrap().clone();
    let connection = s.connection.lock().unwrap().clone();
    SettingsView {
        web_url_effective: web_entry(&p).map(|u| u.to_string()),
        server_url: p.server_url,
        web_url: p.web_url,
        scale: p.scale,
        scales: SCALES,
        reduced_motion: p.reduced_motion,
        click_through: p.click_through,
        start_at_login: p.start_at_login,
        backdrop: p.backdrop,
        visible: p.visible,
        connection,
        platform: platform::capabilities(),
        version: env!("CARGO_PKG_VERSION"),
    }
}

#[tauri::command]
fn settings_get(window: tauri::Window, app: AppHandle) -> Result<SettingsView, String> {
    require(&window, SETTINGS)?;
    Ok(settings_view(&app))
}

#[tauri::command]
async fn settings_set(window: tauri::Window, app: AppHandle, patch: SettingsPatch) -> Result<SettingsView, String> {
    require(&window, SETTINGS)?;
    if let Some(url) = &patch.server_url {
        let origin = ServerOrigin::parse(url).map_err(|e| e.to_string())?;
        let changed = state(&app).prefs.lock().unwrap().server_url.as_deref() != Some(origin.as_str());
        if changed {
            let account = state(&app).account.clone();
            if account.signed_in().await {
                return Err("Sign out before changing the server.".into());
            }
            state(&app).prefs.lock().unwrap().server_url = Some(origin.as_str().to_string());
            let restored = account.restore(origin).await.map_err(|e| e.to_string())?;
            set_connection(&app, if restored { Connection::Connected } else { Connection::SignedOut });
        }
    }
    if let Some(web) = &patch.web_url {
        let value = match web {
            Some(u) if !u.trim().is_empty() => Some(net::validate_web_url(u).map_err(|e| e.to_string())?.to_string()),
            _ => None,
        };
        state(&app).prefs.lock().unwrap().web_url = value;
    }
    if let Some(n) = patch.scale {
        if !SCALES.contains(&n) {
            return Err("unsupported size".into());
        }
        state(&app).prefs.lock().unwrap().scale = n;
    }
    {
        let s = state(&app);
        let mut p = s.prefs.lock().unwrap();
        if let Some(r) = patch.reduced_motion {
            p.reduced_motion = r;
        }
        if let Some(b) = patch.backdrop {
            p.backdrop = b;
        }
        if let Some(c) = patch.click_through {
            p.click_through = c;
        }
    }
    save_prefs(&app);
    apply_window_prefs(&app);
    if let Some(v) = patch.visible {
        set_visible(&app, v);
    }
    if let Some(on) = patch.start_at_login {
        set_autostart(&app, on);
    }
    refresh_menus(&app);
    Ok(settings_view(&app))
}

#[tauri::command]
async fn sign_in(window: tauri::Window, app: AppHandle) -> Result<SettingsView, String> {
    require(&window, SETTINGS)?;
    do_sign_in(&app).await?;
    Ok(settings_view(&app))
}

#[derive(Serialize)]
struct SignOutResult {
    server_confirmed: bool,
    settings: SettingsView,
}

#[tauri::command]
async fn sign_out(window: tauri::Window, app: AppHandle) -> Result<SignOutResult, String> {
    require(&window, SETTINGS)?;
    let confirmed = do_sign_out(&app).await?;
    Ok(SignOutResult { server_confirmed: confirmed, settings: settings_view(&app) })
}

// ── the app ─────────────────────────────────────────────────────────────

fn smoke_arg() -> Option<PathBuf> {
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        if a == "--smoke-test" {
            return args.next().map(PathBuf::from);
        }
        if let Some(p) = a.strip_prefix("--smoke-test=") {
            return Some(PathBuf::from(p));
        }
    }
    None
}

fn finish_smoke(app: &AppHandle) {
    let s = state(app);
    let Some(smoke) = &s.smoke else { return };
    let mut r = smoke.result.lock().unwrap().clone();
    let ok = r["renderer"] == serde_json::json!(true)
        && r["shown"] == serde_json::json!("happy")
        && r["window"] == serde_json::json!(true);
    r["ok"] = serde_json::json!(ok);
    let _ = std::fs::write(&smoke.path, serde_json::to_vec_pretty(&r).unwrap());
    info!("smoke test {}", if ok { "passed" } else { "FAILED" });
    app.exit(if ok { 0 } else { 1 });
}

pub fn run() {
    logging::init();
    let smoke_path = smoke_arg();
    let mut builder = tauri::Builder::default();
    if smoke_path.is_none() {
        builder = builder.plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // Launching the companion again is the guaranteed way back from
            // click-through when no tray exists: it turns click-through off,
            // shows the character and opens Settings.
            set_click_through(app, false);
            set_visible(app, true);
            open_settings(app);
        }));
    }
    builder
        .plugin(tauri_plugin_autostart::init(tauri_plugin_autostart::MacosLauncher::LaunchAgent, None))
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            companion_ready,
            companion_status,
            open_jarvis,
            show_menu,
            start_drag,
            settings_get,
            settings_set,
            sign_in,
            sign_out
        ])
        .setup(move |app| {
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);

            let config_dir =
                app.path().app_config_dir().unwrap_or_else(|_| std::env::temp_dir().join("jarvis-companion"));
            let prefs_path = config_dir.join("prefs.json");
            let prefs = if smoke_path.is_some() { Prefs::default() } else { Prefs::load(&prefs_path) };
            let account = Arc::new(Account::new(Arc::new(OsStore)));
            let smoke = smoke_path.clone().map(|path| Smoke {
                path,
                result: Mutex::new(serde_json::json!({
                    "platform": std::env::consts::OS,
                    "session": platform::display_session(),
                    "renderer": false, "shown": null, "window": false, "tray": false,
                })),
            });
            app.manage(AppState {
                prefs: Mutex::new(prefs.clone()),
                prefs_path,
                account: account.clone(),
                connection: Mutex::new(Connection::NotConfigured),
                wake: tokio::sync::Notify::new(),
                smoke,
            });
            let handle = app.handle().clone();

            // The companion window: transparent, frameless, on top, never
            // taking focus, sized to an integer multiple of the sprite frame.
            let w = WebviewWindowBuilder::new(app, COMPANION, WebviewUrl::App("index.html".into()))
                .title("JARVIS")
                .inner_size((FRAME.0 * prefs.scale) as f64, (FRAME.1 * prefs.scale) as f64)
                .transparent(true)
                .decorations(false)
                .shadow(false)
                .always_on_top(true)
                .skip_taskbar(true)
                .resizable(false)
                .focused(false)
                .visible_on_all_workspaces(true)
                .visible(prefs.visible)
                .build()?;
            restore_position(&handle, &w);
            let _ = w.set_ignore_cursor_events(prefs.click_through);
            if let Some(smoke) = &state(&handle).smoke {
                smoke.result.lock().unwrap()["window"] = serde_json::json!(true);
            }
            let moved = handle.clone();
            w.on_window_event(move |e| {
                if let WindowEvent::Moved(pos) = e {
                    if platform::capabilities().remembers_position {
                        state(&moved).prefs.lock().unwrap().position = Some((pos.x, pos.y));
                        save_prefs(&moved);
                    }
                }
            });

            // The tray, where the platform has one. Its absence is not fatal:
            // right-clicking the character opens the same menu.
            let tray = build_menu(&handle).and_then(|menu| {
                let mut b = TrayIconBuilder::with_id("main").menu(&menu).tooltip("JARVIS");
                if let Some(icon) = app.default_window_icon() {
                    b = b.icon(icon.clone());
                }
                b.build(app)
            });
            match tray {
                Ok(_) => {
                    if let Some(smoke) = &state(&handle).smoke {
                        smoke.result.lock().unwrap()["tray"] = serde_json::json!(true);
                    }
                }
                Err(e) => warn!("no system tray ({e}); the character's right-click menu is the fallback"),
            }
            app.on_menu_event(|app, event| on_menu(app, event.id().as_ref()));

            if state(&handle).smoke.is_some() {
                let h = handle.clone();
                tauri::async_runtime::spawn(async move {
                    let hold = std::env::var("JARVIS_SMOKE_HOLD_MS").ok().and_then(|v| v.parse().ok()).unwrap_or(4000);
                    tokio::time::sleep(Duration::from_millis(hold)).await;
                    finish_smoke(&h);
                });
                tauri::async_runtime::spawn({
                    let h = handle.clone();
                    async move {
                        tokio::time::sleep(Duration::from_secs(60)).await;
                        finish_smoke(&h);
                    }
                });
                return Ok(());
            }

            // Restore this server's device credential, then poll.
            let h = handle.clone();
            tauri::async_runtime::spawn(async move {
                let server = state(&h).prefs.lock().unwrap().server_url.clone();
                let connection = match server.as_deref().map(ServerOrigin::parse) {
                    None => Connection::NotConfigured,
                    Some(Err(_)) => Connection::Error("the saved server address is invalid".into()),
                    Some(Ok(origin)) => match account.restore(origin).await {
                        Ok(true) => Connection::Connected,
                        Ok(false) => Connection::SignedOut,
                        Err(e) => Connection::Error(e.to_string()),
                    },
                };
                set_connection(&h, connection);
                poller(h).await;
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running the JARVIS companion");
}

#[cfg(test)]
mod tests;
