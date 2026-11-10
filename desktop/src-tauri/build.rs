// The app's own commands become permissions (`allow-<command>`), which the
// capability files grant per window — nothing is reachable from a webview
// unless it is listed in both places (src/lib.rs COMPANION_COMMANDS /
// SETTINGS_COMMANDS, checked by the tests).
fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(&[
        "companion_ready",
        "companion_status",
        "open_jarvis",
        "show_menu",
        "start_drag",
        "settings_get",
        "settings_set",
        "sign_in",
        "sign_out",
    ])))
    .expect("failed to run tauri-build");
}
