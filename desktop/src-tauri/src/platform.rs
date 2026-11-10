//! What the operating system can tell the companion, and what it cannot.
//!
//! * `screen_locked()` — polling stops while the session is locked and a
//!   fresh report is fetched on unlock. macOS: the window server's session
//!   dictionary; Windows: the WTS session state; Linux: logind's
//!   `LockedHint`. `None` when the platform cannot say (treated as unlocked,
//!   so the companion degrades to "polls while visible").
//! * `DisplaySession` — on Linux, X11 or Wayland, which decides what window
//!   behaviour can be promised (docs/CHARACTER_DESKTOP_PROPOSAL.md §6).
//!
//! Nothing here reads input, window titles, the clipboard or the screen.

use serde::Serialize;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DisplaySession {
    Windows,
    Macos,
    X11,
    Wayland,
    Unknown,
}

pub fn display_session() -> DisplaySession {
    if cfg!(target_os = "windows") {
        return DisplaySession::Windows;
    }
    if cfg!(target_os = "macos") {
        return DisplaySession::Macos;
    }
    let forced_x11 = std::env::var("GDK_BACKEND").map(|v| v.starts_with("x11")).unwrap_or(false);
    let session = std::env::var("XDG_SESSION_TYPE").unwrap_or_default();
    if !forced_x11 && (session == "wayland" || std::env::var_os("WAYLAND_DISPLAY").is_some()) {
        DisplaySession::Wayland
    } else if std::env::var_os("DISPLAY").is_some() || session == "x11" {
        DisplaySession::X11
    } else {
        DisplaySession::Unknown
    }
}

/// What this session lets the companion do, shown in Settings so a
/// limitation is stated rather than discovered.
#[derive(Debug, Clone, Serialize)]
pub struct Capabilities {
    pub session: DisplaySession,
    pub always_on_top: &'static str,
    pub remembers_position: bool,
    pub click_through: &'static str,
    pub notes: Vec<&'static str>,
}

pub fn capabilities() -> Capabilities {
    match display_session() {
        DisplaySession::Wayland => Capabilities {
            session: DisplaySession::Wayland,
            always_on_top: "compositor-dependent (GNOME ignores it; KDE honours window rules)",
            remembers_position: false,
            click_through: "compositor-dependent; turn it off from the tray or by launching JARVIS Companion again",
            notes: vec![
                "Wayland does not let an app place itself or stay above other windows; the companion is a small movable window here.",
                "To get X11 behaviour under XWayland, start it with GDK_BACKEND=x11 (opt-in).",
                "Without a tray (GNOME without an AppIndicator extension), right-click the character for its menu.",
            ],
        },
        DisplaySession::X11 => Capabilities {
            session: DisplaySession::X11,
            always_on_top: "yes (window-manager dependent)",
            remembers_position: true,
            click_through: "yes",
            notes: vec![
                "Transparency needs a running compositor; without one, turn on Backdrop in Settings.",
                "If the character is blank on NVIDIA, start it with WEBKIT_DISABLE_DMABUF_RENDERER=1.",
            ],
        },
        DisplaySession::Macos => Capabilities {
            session: DisplaySession::Macos,
            always_on_top: "yes (floating level, on every Space)",
            remembers_position: true,
            click_through: "yes",
            notes: vec!["Covered by full-screen apps in their own Space."],
        },
        DisplaySession::Windows => Capabilities {
            session: DisplaySession::Windows,
            always_on_top: "yes (covered by exclusive full-screen apps)",
            remembers_position: true,
            click_through: "yes",
            notes: vec![],
        },
        DisplaySession::Unknown => Capabilities {
            session: DisplaySession::Unknown,
            always_on_top: "unknown",
            remembers_position: false,
            click_through: "unknown",
            notes: vec!["No display session was detected."],
        },
    }
}

#[cfg(target_os = "macos")]
pub fn screen_locked() -> Option<bool> {
    use core_foundation::base::{CFType, TCFType};
    use core_foundation::boolean::CFBoolean;
    use core_foundation::dictionary::{CFDictionary, CFDictionaryRef};
    use core_foundation::string::CFString;

    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGSessionCopyCurrentDictionary() -> CFDictionaryRef;
    }
    // SAFETY: a Copy-rule CoreFoundation call; a null result means no session.
    let raw = unsafe { CGSessionCopyCurrentDictionary() };
    if raw.is_null() {
        return None;
    }
    // SAFETY: we own the +1 reference returned by the Copy function.
    let dict: CFDictionary<CFString, CFType> = unsafe { CFDictionary::wrap_under_create_rule(raw) };
    let locked = dict
        .find(CFString::from_static_string("CGSSessionScreenIsLocked"))
        .and_then(|v| v.downcast::<CFBoolean>())
        .map(bool::from)
        .unwrap_or(false);
    Some(locked)
}

#[cfg(target_os = "windows")]
pub fn screen_locked() -> Option<bool> {
    use windows::Win32::System::RemoteDesktop::{
        WTSFreeMemory, WTSQuerySessionInformationW, WTSSessionInfoEx, WTSINFOEXW, WTS_CURRENT_SERVER_HANDLE,
        WTS_CURRENT_SESSION,
    };
    // WTS_SESSIONSTATE_LOCK (0) / _UNLOCK (1), Windows 8+ semantics.
    const WTS_SESSIONSTATE_LOCK: i32 = 0;
    let mut buffer = windows::core::PWSTR::null();
    let mut bytes = 0u32;
    // SAFETY: documented out-parameters; the buffer is freed below.
    let ok = unsafe {
        WTSQuerySessionInformationW(
            Some(WTS_CURRENT_SERVER_HANDLE),
            WTS_CURRENT_SESSION,
            WTSSessionInfoEx,
            &mut buffer,
            &mut bytes,
        )
    };
    if ok.is_err() || buffer.is_null() || (bytes as usize) < std::mem::size_of::<WTSINFOEXW>() {
        return None;
    }
    // SAFETY: the API returned a WTSINFOEXW of at least that size.
    let info = unsafe { &*(buffer.0 as *const WTSINFOEXW) };
    let locked = if info.Level == 1 {
        // SAFETY: Level 1 selects the WTSInfoExLevel1 member.
        unsafe { info.Data.WTSInfoExLevel1.SessionFlags == WTS_SESSIONSTATE_LOCK }
    } else {
        false
    };
    // SAFETY: frees the buffer WTSQuerySessionInformationW allocated.
    unsafe { WTSFreeMemory(buffer.0 as *mut _) };
    Some(locked)
}

#[cfg(target_os = "linux")]
pub fn screen_locked() -> Option<bool> {
    use dbus::blocking::stdintf::org_freedesktop_dbus::Properties;
    use dbus::blocking::Connection;
    let conn = Connection::new_system().ok()?;
    let proxy = conn.with_proxy(
        "org.freedesktop.login1",
        "/org/freedesktop/login1/session/auto",
        std::time::Duration::from_millis(500),
    );
    proxy.get::<bool>("org.freedesktop.login1.Session", "LockedHint").ok()
}

#[cfg(not(any(target_os = "macos", target_os = "windows", target_os = "linux")))]
pub fn screen_locked() -> Option<bool> {
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn capabilities_never_promise_wayland_positioning() {
        let caps = capabilities();
        if caps.session == DisplaySession::Wayland {
            assert!(!caps.remembers_position);
        }
        // The lock query must not panic wherever the tests run.
        let _ = screen_locked();
    }
}
