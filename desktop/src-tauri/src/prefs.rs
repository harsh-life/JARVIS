//! Local, non-secret preferences (a small JSON file in the app's config dir).
//!
//! Holds only presentation settings and the server's address. Never a
//! credential, a token, a report, a score or an expression. A damaged file is
//! set aside (renamed `.corrupt`) and the defaults apply; unknown fields are
//! dropped; out-of-range values are clamped to the defaults.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

pub const SCALES: [u32; 3] = [2, 3, 4];

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReducedMotion {
    System,
    Always,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Prefs {
    /// The JARVIS server's origin (self-hosted). None until configured.
    pub server_url: Option<String>,
    /// The page "Open JARVIS" opens. None: the server's `/ui/` (the
    /// Intelligence Workbench — a configuration page, not a chat).
    pub web_url: Option<String>,
    pub scale: u32,
    pub reduced_motion: ReducedMotion,
    pub click_through: bool,
    pub start_at_login: bool,
    /// A soft plate behind the character, for desktops without a compositor
    /// (X11 without one draws transparent windows black).
    pub backdrop: bool,
    pub visible: bool,
    /// Last position, where the platform lets an app know and set it
    /// (never on Wayland).
    pub position: Option<(i32, i32)>,
}

impl Default for Prefs {
    fn default() -> Self {
        Prefs {
            server_url: None,
            web_url: None,
            scale: 2,
            reduced_motion: ReducedMotion::System,
            click_through: false,
            start_at_login: false,
            backdrop: false,
            visible: true,
            position: None,
        }
    }
}

impl Prefs {
    pub fn sanitized(mut self) -> Self {
        if !SCALES.contains(&self.scale) {
            self.scale = Prefs::default().scale;
        }
        if let Some((x, y)) = self.position {
            if x.abs() > 100_000 || y.abs() > 100_000 {
                self.position = None;
            }
        }
        self
    }

    pub fn load(path: &Path) -> Self {
        let Ok(text) = std::fs::read_to_string(path) else { return Prefs::default() };
        match serde_json::from_str::<Prefs>(&text) {
            Ok(p) => p.sanitized(),
            Err(_) => {
                log::warn!("preferences were unreadable; using defaults (kept as .corrupt)");
                let _ = std::fs::rename(path, path.with_extension("corrupt"));
                Prefs::default()
            }
        }
    }

    pub fn save(&self, path: &Path) -> std::io::Result<()> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let tmp: PathBuf = path.with_extension("tmp");
        std::fs::write(&tmp, serde_json::to_vec_pretty(self).expect("serialisable"))?;
        std::fs::rename(tmp, path)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn corrupt_and_hostile_files_fall_back_safely() {
        let dir = std::env::temp_dir().join(format!("jarvis-prefs-{}", std::process::id()));
        let path = dir.join("prefs.json");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(&path, "{not json").unwrap();
        assert_eq!(Prefs::load(&path), Prefs::default());
        assert!(path.with_extension("corrupt").exists());

        std::fs::write(&path, r#"{"scale": 99, "position": [999999, 0], "access_token": "x", "visible": false}"#)
            .unwrap();
        let p = Prefs::load(&path);
        assert_eq!(p.scale, 2);
        assert_eq!(p.position, None);
        assert!(!p.visible);

        p.save(&path).unwrap();
        let text = std::fs::read_to_string(&path).unwrap();
        assert!(!text.contains("access_token"), "unknown fields are not carried");
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn defaults_are_conservative() {
        let p = Prefs::default();
        assert!(!p.start_at_login && !p.click_through && p.server_url.is_none());
    }
}
