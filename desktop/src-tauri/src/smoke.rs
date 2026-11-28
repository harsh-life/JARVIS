//! `--smoke-test <result.json>`: a one-shot check of the packaged app.
//!
//! The run opens the real companion window, loads the sprite atlas in the
//! packaged webview, feeds a synthetic report through the shared policy,
//! creates the tray, writes one result file and then **exits**: 0 when every
//! required check passed, 1 otherwise. CI (`.github/workflows/desktop.yml`)
//! asserts both halves separately: the result file says `ok: true`, and the
//! process terminated with status 0 within a bounded time.
//!
//! The rules live here, apart from the Tauri wiring in `lib.rs`, so they can
//! be tested without a window.

use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use serde_json::Value;

/// How long the app waits for its own graceful shutdown (`AppHandle::exit`,
/// which only *asks* the event loop to unwind) before it ends the process
/// itself with the same status. The result file is already written and
/// nothing else in smoke mode holds state that needs flushing.
pub const FORCED_EXIT_AFTER: Duration = Duration::from_secs(10);

/// The latest a run reports: a check that has not completed by then is a
/// failure, not a hang.
pub const DEADLINE: Duration = Duration::from_secs(60);

/// Whether the platform always has somewhere to put a tray icon. Windows has
/// the notification area and macOS the menu bar, so a missing tray there is a
/// failure. On Linux it is a supported configuration (GNOME without an
/// AppIndicator extension; the character's right-click menu is the fallback,
/// docs/CHARACTER_DESKTOP_PROPOSAL.md §6), so it is recorded, not required.
pub fn tray_required(os: &str) -> bool {
    matches!(os, "windows" | "macos")
}

/// Whether a run passed: the window was created, the atlas loaded
/// (`renderer`), the policy showed the expected expression for the synthetic
/// report, and the tray was created where the platform guarantees one.
pub fn verdict(result: &Value, os: &str) -> bool {
    let yes = |key: &str| result[key] == Value::Bool(true);
    yes("window") && yes("renderer") && result["shown"] == "happy" && (!tray_required(os) || yes("tray"))
}

pub fn exit_code(ok: bool) -> i32 {
    if ok {
        0
    } else {
        1
    }
}

/// Lets exactly one caller finish the run. The hold timer and the deadline
/// both try; whichever comes second must not write a second result, report a
/// second time, or override the first exit status.
#[derive(Default)]
pub struct Once(AtomicBool);

impl Once {
    pub fn claim(&self) -> bool {
        !self.0.swap(true, Ordering::SeqCst)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn passing() -> Value {
        json!({"window": true, "renderer": true, "shown": "happy", "intensity": 8, "tray": true})
    }

    #[test]
    fn a_complete_run_passes_on_every_platform() {
        for os in ["windows", "macos", "linux"] {
            assert!(verdict(&passing(), os), "{os}");
            assert_eq!(exit_code(verdict(&passing(), os)), 0);
        }
    }

    #[test]
    fn each_required_check_fails_the_run_on_its_own() {
        for (key, bad) in
            [("window", json!(false)), ("renderer", json!(false)), ("shown", json!(null)), ("shown", json!("sad"))]
        {
            let mut r = passing();
            r[key] = bad.clone();
            for os in ["windows", "macos", "linux"] {
                assert!(!verdict(&r, os), "{key}={bad} on {os}");
                assert_eq!(exit_code(verdict(&r, os)), 1);
            }
        }
    }

    #[test]
    fn the_initial_record_fails() {
        // What lib.rs records before any check has run (the deadline case).
        let r = json!({"renderer": false, "shown": null, "window": false, "tray": false});
        for os in ["windows", "macos", "linux"] {
            assert!(!verdict(&r, os));
        }
    }

    #[test]
    fn a_missing_tray_fails_only_where_a_tray_always_exists() {
        let mut r = passing();
        r["tray"] = json!(false);
        assert!(!verdict(&r, "windows"));
        assert!(!verdict(&r, "macos"));
        assert!(verdict(&r, "linux"));
    }

    #[test]
    fn only_the_first_finish_counts() {
        let once = Once::default();
        assert!(once.claim());
        assert!(!once.claim());
        assert!(!once.claim());
    }

    #[test]
    fn a_smoke_run_never_saves_preferences() {
        // Every preference write goes through `save_prefs`, which returns
        // before touching the file in smoke mode.
        let lib = include_str!("lib.rs");
        let body = lib.split("fn save_prefs(app: &AppHandle) {").nth(1).expect("save_prefs exists");
        let guard = body.find("if s.smoke.is_some() {").expect("smoke guard present");
        let write = body.find(".save(&s.prefs_path)").expect("save call present");
        assert!(guard < write, "the smoke guard must come before the write");
        assert_eq!(lib.matches(".save(&").count(), 1, "prefs are saved only in save_prefs");
    }

    #[test]
    fn the_forced_exit_comes_well_before_the_ci_bound() {
        // CI allows 90 s from launch: the longest hold (8 s) plus the grace
        // period must fit with room to spare, and so must the deadline path.
        assert!(Duration::from_secs(8) + FORCED_EXIT_AFTER < Duration::from_secs(90));
        assert!(DEADLINE + FORCED_EXIT_AFTER < Duration::from_secs(90));
    }
}
