//! The loopback listener the system browser returns the login to
//! (RFC 8252 §7.3; OD-DESK-1).
//!
//! Bound to 127.0.0.1 on an ephemeral port, for one login only. It accepts
//! exactly `GET /jarvis/callback?code=…&state=…` whose `state` equals the
//! companion's own nonce (compared in constant time); anything else is
//! answered and ignored, so a stray or hostile request cannot end the login.
//! The page it serves contains no script. The code it receives is worthless
//! without the PKCE verifier, which never left this process.

use std::time::Duration;

use subtle::ConstantTimeEq;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use zeroize::Zeroizing;

pub const CALLBACK_PATH: &str = "/jarvis/callback";
const MAX_REQUEST: usize = 8 * 1024;
const MAX_ATTEMPTS: usize = 64;

const DONE_PAGE: &str = "<!doctype html><meta charset=utf-8><title>JARVIS</title>\
<p>Signed in. You can close this tab and return to the JARVIS companion.</p>";
const REFUSED_PAGE: &str = "<!doctype html><meta charset=utf-8><title>JARVIS</title>\
<p>This sign-in was not started by this JARVIS companion.</p>";

pub struct Loopback {
    listener: TcpListener,
    pub port: u16,
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum LoopbackError {
    #[error("could not open a local port for the sign-in to return to")]
    Bind,
    #[error("the sign-in was not completed in time")]
    TimedOut,
    #[error("too many unexpected requests reached the sign-in listener")]
    Flooded,
}

impl Loopback {
    pub async fn bind() -> Result<Self, LoopbackError> {
        let listener = TcpListener::bind(("127.0.0.1", 0)).await.map_err(|_| LoopbackError::Bind)?;
        let port = listener.local_addr().map_err(|_| LoopbackError::Bind)?.port();
        Ok(Loopback { listener, port })
    }

    /// Wait for the browser to return with this login's state; give back the code.
    pub async fn wait(self, expected_state: &str, timeout: Duration) -> Result<Zeroizing<String>, LoopbackError> {
        let deadline = tokio::time::Instant::now() + timeout;
        for _ in 0..MAX_ATTEMPTS {
            let accepted =
                tokio::time::timeout_at(deadline, self.listener.accept()).await.map_err(|_| LoopbackError::TimedOut)?;
            let Ok((stream, _)) = accepted else { continue };
            if let Some(code) = handle(stream, expected_state).await {
                return Ok(code);
            }
        }
        Err(LoopbackError::Flooded)
    }
}

async fn handle(mut stream: TcpStream, expected_state: &str) -> Option<Zeroizing<String>> {
    let mut buf = Zeroizing::new(vec![0u8; MAX_REQUEST]);
    let mut len = 0;
    let read = tokio::time::timeout(Duration::from_secs(5), async {
        while len < MAX_REQUEST {
            let n = stream.read(&mut buf[len..]).await.ok()?;
            if n == 0 {
                break;
            }
            len += n;
            if buf[..len].windows(4).any(|w| w == b"\r\n\r\n") {
                break;
            }
        }
        Some(())
    })
    .await;
    if read.is_err() {
        return None;
    }
    let head = std::str::from_utf8(&buf[..len]).ok()?;
    let line = head.lines().next()?;
    let mut parts = line.split(' ');
    let (method, target) = (parts.next()?, parts.next()?);
    let outcome = if method == "GET" { parse_callback(target, expected_state) } else { None };
    let (status, body) = match (&outcome, target.split('?').next()) {
        (Some(_), _) => ("200 OK", DONE_PAGE),
        (None, Some(CALLBACK_PATH)) => ("400 Bad Request", REFUSED_PAGE),
        _ => ("404 Not Found", ""),
    };
    let response = format!(
        "HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\n\
Cache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nContent-Security-Policy: default-src 'none'\r\n\
Connection: close\r\n\r\n{body}",
        body.len()
    );
    let _ = stream.write_all(response.as_bytes()).await;
    let _ = stream.shutdown().await;
    outcome
}

/// `"/jarvis/callback?code=…&state=…"` → the code, if the state is ours.
pub fn parse_callback(target: &str, expected_state: &str) -> Option<Zeroizing<String>> {
    let (path, query) = target.split_once('?')?;
    if path != CALLBACK_PATH {
        return None;
    }
    let mut code = None;
    let mut state = None;
    for pair in query.split('&') {
        let (k, v) = pair.split_once('=')?;
        // Both values are URL-safe base64 by construction; anything that
        // would need decoding is not one of ours.
        if !v.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_') || v.is_empty() {
            return None;
        }
        match k {
            "code" if code.is_none() => code = Some(v),
            "state" if state.is_none() => state = Some(v),
            _ => return None,
        }
    }
    let state = state?;
    if state.len() != expected_state.len() || !bool::from(state.as_bytes().ct_eq(expected_state.as_bytes())) {
        return None;
    }
    code.map(|c| Zeroizing::new(c.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_our_state_is_accepted() {
        let s = "S".repeat(43);
        assert_eq!(
            parse_callback(&format!("/jarvis/callback?code=abc_D-1&state={s}"), &s).unwrap().as_str(),
            "abc_D-1"
        );
        assert!(parse_callback(&format!("/jarvis/callback?code=abc&state={}", "T".repeat(43)), &s).is_none());
        assert!(parse_callback("/jarvis/callback?code=abc", &s).is_none());
        assert!(parse_callback(&format!("/other?code=abc&state={s}"), &s).is_none());
        assert!(parse_callback(&format!("/jarvis/callback?code=a%20b&state={s}"), &s).is_none());
        assert!(parse_callback(&format!("/jarvis/callback?code=a&code=b&state={s}"), &s).is_none());
        assert!(parse_callback(&format!("/jarvis/callback?code=a&state={s}&x=1"), &s).is_none());
    }

    async fn get(port: u16, target: &str) -> String {
        let mut s = TcpStream::connect(("127.0.0.1", port)).await.unwrap();
        s.write_all(format!("GET {target} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n").as_bytes()).await.unwrap();
        let mut out = String::new();
        s.read_to_string(&mut out).await.unwrap();
        out
    }

    #[tokio::test]
    async fn a_wrong_state_does_not_end_the_login_and_the_page_has_no_script() {
        let lb = Loopback::bind().await.unwrap();
        let port = lb.port;
        let state = "Z".repeat(43);
        let waiter = tokio::spawn({
            let state = state.clone();
            async move { lb.wait(&state, Duration::from_secs(10)).await }
        });
        let refused = get(port, &format!("/jarvis/callback?code=evil&state={}", "Y".repeat(43))).await;
        assert!(refused.starts_with("HTTP/1.1 400"));
        assert!(get(port, "/favicon.ico").await.starts_with("HTTP/1.1 404"));
        let ok = get(port, &format!("/jarvis/callback?code=good-code&state={state}")).await;
        assert!(ok.starts_with("HTTP/1.1 200"));
        assert!(!ok.to_lowercase().contains("<script"));
        assert!(ok.contains("Content-Security-Policy: default-src 'none'"));
        assert_eq!(waiter.await.unwrap().unwrap().as_str(), "good-code");
        // Single use: the listener is gone.
        assert!(TcpStream::connect(("127.0.0.1", port)).await.is_err());
    }

    #[tokio::test]
    async fn it_times_out() {
        let lb = Loopback::bind().await.unwrap();
        assert_eq!(lb.wait("x", Duration::from_millis(50)).await.unwrap_err(), LoopbackError::TimedOut);
    }
}
