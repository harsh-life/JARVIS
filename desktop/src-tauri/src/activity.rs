//! The activity report (`GET /api/v1/presentation/activity`, OD-CHAR-4),
//! validated whole in Rust before anything crosses into the webview.
//!
//! The webview validates it again with the shared JavaScript parser
//! (`shared/web/activity-policy.js`); both are held to
//! `shared/android/presentation_activity_samples.json`. What crosses the IPC
//! boundary is this module's *re-serialised* report — never the raw response,
//! never a header, never a credential. Reports are not stored or logged.

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Scores {
    pub happy: u8,
    pub satisfied: u8,
    pub default: u8,
    pub sleepy: u8,
    pub surprised: u8,
    pub angry: u8,
    pub sad: u8,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Components {
    pub engagement: u8,
    pub productivity: u8,
    pub steadiness: u8,
    pub failure: u8,
    pub pressure: u8,
    pub surprise: u8,
    pub inactivity: u8,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ActivityReport {
    pub schema_version: u8,
    pub computed_at: String,
    pub window_s: u32,
    pub scores: Scores,
    pub components: Components,
}

impl ActivityReport {
    /// Parse and validate a response body. `None` for anything that is not a
    /// whole, in-range report.
    pub fn parse(body: &[u8]) -> Option<Self> {
        let report: ActivityReport = serde_json::from_slice(body).ok()?;
        report.valid().then_some(report)
    }

    fn valid(&self) -> bool {
        let s = &self.scores;
        let c = &self.components;
        self.schema_version == 1
            && (60..=86_400).contains(&self.window_s)
            && [s.happy, s.satisfied, s.default, s.sleepy, s.surprised, s.angry, s.sad]
                .iter()
                .all(|v| (1..=10).contains(v))
            && [c.engagement, c.productivity, c.steadiness, c.failure, c.pressure, c.surprise, c.inactivity]
                .iter()
                .all(|v| *v <= 100)
            && aware_timestamp(&self.computed_at)
    }

    /// The canonical JSON text handed to the webview.
    pub fn to_json(&self) -> String {
        serde_json::to_string(self).expect("serialisable")
    }
}

/// RFC 3339 date-time with a zone: `YYYY-MM-DDTHH:MM[:SS[.f{1,9}]](Z|±HH:MM)`,
/// with the calendar checked — the same rule as the JavaScript parser.
pub fn aware_timestamp(text: &str) -> bool {
    let b = text.as_bytes();
    let digits = |r: std::ops::Range<usize>| b.get(r.clone()).is_some_and(|s| s.iter().all(u8::is_ascii_digit));
    let num = |r: std::ops::Range<usize>| -> u32 { std::str::from_utf8(&b[r]).unwrap().parse().unwrap() };
    if b.len() < 17
        || !digits(0..4)
        || b[4] != b'-'
        || !digits(5..7)
        || b[7] != b'-'
        || !digits(8..10)
        || b[10] != b'T'
        || !digits(11..13)
        || b[13] != b':'
        || !digits(14..16)
    {
        return false;
    }
    let (year, month, day, hour, minute) = (num(0..4), num(5..7), num(8..10), num(11..13), num(14..16));
    let mut i = 16;
    if b.get(i) == Some(&b':') {
        if !digits(i + 1..i + 3) || num(i + 1..i + 3) > 59 {
            return false;
        }
        i += 3;
        if b.get(i) == Some(&b'.') {
            let start = i + 1;
            i = start;
            while b.get(i).is_some_and(u8::is_ascii_digit) {
                i += 1;
            }
            if i == start || i - start > 9 {
                return false;
            }
        }
    }
    let zone_ok = match b.get(i) {
        Some(b'Z') | Some(b'z') => i + 1 == b.len(),
        Some(b'+') | Some(b'-') => {
            i + 6 == b.len()
                && digits(i + 1..i + 3)
                && b[i + 3] == b':'
                && digits(i + 4..i + 6)
                && num(i + 1..i + 3) <= 23
                && num(i + 4..i + 6) <= 59
        }
        _ => false,
    };
    let leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
    let days = [31, if leap { 29 } else { 28 }, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    zone_ok
        && year >= 1
        && (1..=12).contains(&month)
        && day >= 1
        && day <= days[(month - 1) as usize]
        && hour <= 23
        && minute <= 59
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn samples() -> Value {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../shared/android/presentation_activity_samples.json");
        serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
    }

    #[test]
    fn every_shared_sample() {
        let s = samples();
        for v in s["valid"].as_array().unwrap() {
            assert!(ActivityReport::parse(v["text"].as_str().unwrap().as_bytes()).is_some(), "{}", v["name"]);
        }
        for v in s["invalid"].as_array().unwrap() {
            assert!(ActivityReport::parse(v["text"].as_str().unwrap().as_bytes()).is_none(), "{}", v["name"]);
        }
    }

    #[test]
    fn what_crosses_to_the_webview_is_the_canonical_report_only() {
        let s = samples();
        let text = s["valid"][0]["text"].as_str().unwrap();
        let report = ActivityReport::parse(text.as_bytes()).unwrap();
        let out: Value = serde_json::from_str(&report.to_json()).unwrap();
        let keys: Vec<_> = out.as_object().unwrap().keys().cloned().collect();
        assert_eq!(keys.len(), 5);
        assert_eq!(ActivityReport::parse(report.to_json().as_bytes()), Some(report));
    }

    #[test]
    fn near_misses() {
        let ok = r#"{"schema_version":1,"computed_at":"2026-11-10T12:00:00Z","window_s":1800,"scores":{"happy":8,"satisfied":8,"default":5,"sleepy":1,"surprised":1,"angry":2,"sad":1},"components":{"engagement":1,"productivity":1,"steadiness":1,"failure":0,"pressure":0,"surprise":0,"inactivity":0}}"#;
        assert!(ActivityReport::parse(ok.as_bytes()).is_some());
        for bad in [
            ok.replace("\"happy\":8", "\"happy\":8.0"),
            ok.replace("1800", "1.8e3"),
            ok.replace("2026-11-10T12", "2026-02-30T12"),
            ok.replace("T12:00:00Z", "T12:00:00"),
        ] {
            assert!(ActivityReport::parse(bad.as_bytes()).is_none(), "{bad}");
        }
        assert!(aware_timestamp("2026-11-10T12:00:00.123456+05:30"));
        assert!(aware_timestamp("2026-11-10T12:00Z"));
        assert!(!aware_timestamp("2026-11-10 12:00:00Z"));
        assert!(!aware_timestamp("2026-13-10T12:00:00Z"));
    }
}
