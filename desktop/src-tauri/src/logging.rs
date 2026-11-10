//! A minimal stderr logger. Nothing secret is ever passed to it — no token,
//! proof, key, code, verifier, report, score or expression — and the tests
//! capture every line of a full sign-in to prove it (`tests` in `lib.rs`).

use std::sync::Mutex;

use log::{Level, LevelFilter, Log, Metadata, Record};

static CAPTURE: Mutex<Option<Vec<String>>> = Mutex::new(None);

struct Logger;

impl Log for Logger {
    fn enabled(&self, metadata: &Metadata) -> bool {
        metadata.level() <= Level::Info || metadata.target().starts_with("jarvis_companion")
    }

    fn log(&self, record: &Record) {
        if !self.enabled(record.metadata()) {
            return;
        }
        let line = format!("[{}] {}: {}", record.level(), record.target(), record.args());
        if let Ok(mut capture) = CAPTURE.lock() {
            if let Some(lines) = capture.as_mut() {
                lines.push(line.clone());
            }
        }
        eprintln!("{line}");
    }

    fn flush(&self) {}
}

pub fn init() {
    if log::set_logger(&Logger).is_ok() {
        log::set_max_level(LevelFilter::Debug);
    }
}

/// Start collecting every log line (tests).
pub fn capture() {
    init();
    *CAPTURE.lock().unwrap() = Some(Vec::new());
}

pub fn captured() -> Vec<String> {
    CAPTURE.lock().unwrap().clone().unwrap_or_default()
}
