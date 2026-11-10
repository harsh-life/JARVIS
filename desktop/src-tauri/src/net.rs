//! The configured JARVIS server and the HTTP client that talks to it.
//!
//! * The server is an **origin** the user configures (self-hosting): HTTPS, or
//!   plain HTTP only for a loopback development server. No path, query,
//!   fragment or credentials. There is no built-in production URL.
//! * The client **never follows a redirect**, so an `Authorization` header can
//!   never be forwarded to another origin; a 3xx is an error.
//! * Every request has bounded connect and total timeouts, and every body is
//!   read with a size cap.

use std::time::Duration;

use reqwest::{redirect, Client, Response, StatusCode};
use url::{Host, Url};

pub const MAX_BODY: usize = 64 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServerOrigin {
    origin: Url,
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum OriginError {
    #[error("not a URL")]
    NotAUrl,
    #[error("the server must use https (plain http is allowed only for localhost development)")]
    Insecure,
    #[error("give the server's origin only, like https://jarvis.example.com — no path, query or fragment")]
    NotAnOrigin,
    #[error("the URL must not contain a user name or password")]
    Credentials,
}

fn is_loopback(host: &Host<&str>) -> bool {
    match host {
        Host::Domain(d) => d.eq_ignore_ascii_case("localhost"),
        Host::Ipv4(ip) => ip.is_loopback(),
        Host::Ipv6(ip) => ip.is_loopback(),
    }
}

impl ServerOrigin {
    pub fn parse(text: &str) -> Result<Self, OriginError> {
        let url = Url::parse(text.trim()).map_err(|_| OriginError::NotAUrl)?;
        if !url.username().is_empty() || url.password().is_some() {
            return Err(OriginError::Credentials);
        }
        let host = url.host().ok_or(OriginError::NotAUrl)?;
        match url.scheme() {
            "https" => {}
            "http" if is_loopback(&host) => {}
            "http" => return Err(OriginError::Insecure),
            _ => return Err(OriginError::NotAUrl),
        }
        if !(url.path() == "/" || url.path().is_empty()) || url.query().is_some() || url.fragment().is_some() {
            return Err(OriginError::NotAnOrigin);
        }
        let mut origin = url;
        origin.set_path("/");
        Ok(ServerOrigin { origin })
    }

    pub fn is_https(&self) -> bool {
        self.origin.scheme() == "https"
    }

    pub fn as_str(&self) -> &str {
        self.origin.as_str().trim_end_matches('/')
    }

    /// An API URL on this origin. `path` is a fixed string from this crate.
    pub fn api(&self, path: &str) -> Url {
        self.origin.join(&format!("api/v1/{}", path.trim_start_matches('/'))).expect("static API path")
    }

    pub fn same_origin(&self, other: &Url) -> bool {
        self.origin.origin() == other.origin()
    }
}

/// A web page the companion may open in the browser: same rules as the server
/// (https, or http on loopback), any path. Never carries a credential.
pub fn validate_web_url(text: &str) -> Result<Url, OriginError> {
    let url = Url::parse(text.trim()).map_err(|_| OriginError::NotAUrl)?;
    if !url.username().is_empty() || url.password().is_some() {
        return Err(OriginError::Credentials);
    }
    let host = url.host().ok_or(OriginError::NotAUrl)?;
    match url.scheme() {
        "https" => Ok(url),
        "http" if is_loopback(&host) => Ok(url),
        "http" => Err(OriginError::Insecure),
        _ => Err(OriginError::NotAUrl),
    }
}

pub fn client(origin: &ServerOrigin) -> Client {
    Client::builder()
        .redirect(redirect::Policy::none())
        .connect_timeout(Duration::from_secs(8))
        .timeout(Duration::from_secs(20))
        .https_only(origin.is_https())
        .user_agent(concat!("JARVIS-Desktop-Companion/", env!("CARGO_PKG_VERSION")))
        .build()
        .expect("HTTP client")
}

#[derive(Debug, thiserror::Error)]
pub enum NetError {
    #[error("the server could not be reached")]
    Unreachable,
    #[error("the server answered {0}")]
    Status(StatusCode),
    #[error("the server redirected; redirects are never followed")]
    Redirected,
    #[error("the server's answer was too large or malformed")]
    BadBody,
}

impl NetError {
    pub fn is_unauthorized(&self) -> bool {
        matches!(self, NetError::Status(s) if *s == StatusCode::UNAUTHORIZED)
    }
}

impl From<reqwest::Error> for NetError {
    fn from(_: reqwest::Error) -> Self {
        // Deliberately dropped: a reqwest error can quote the URL, and the
        // only URLs here are the server's, but nothing about the request
        // needs to reach a log.
        NetError::Unreachable
    }
}

/// Check the status and read a bounded body.
pub async fn read_body(resp: Response) -> Result<Vec<u8>, NetError> {
    let status = resp.status();
    if status.is_redirection() {
        return Err(NetError::Redirected);
    }
    if !status.is_success() {
        return Err(NetError::Status(status));
    }
    if resp.content_length().is_some_and(|n| n as usize > MAX_BODY) {
        return Err(NetError::BadBody);
    }
    let mut resp = resp;
    let mut body = Vec::new();
    while let Some(chunk) = resp.chunk().await? {
        if body.len() + chunk.len() > MAX_BODY {
            return Err(NetError::BadBody);
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn origins() {
        for ok in [
            "https://jarvis.example.com",
            "https://jarvis.example.com/",
            "https://10.0.0.2:8443",
            "http://localhost:8000",
            "http://127.0.0.1:8000/",
            "http://[::1]:8000",
        ] {
            assert!(ServerOrigin::parse(ok).is_ok(), "{ok}");
        }
        assert_eq!(ServerOrigin::parse("http://jarvis.example.com"), Err(OriginError::Insecure));
        assert_eq!(ServerOrigin::parse("http://192.168.1.4:8000"), Err(OriginError::Insecure));
        assert_eq!(ServerOrigin::parse("https://jarvis.example.com/api"), Err(OriginError::NotAnOrigin));
        assert_eq!(ServerOrigin::parse("https://jarvis.example.com/?x=1"), Err(OriginError::NotAnOrigin));
        assert_eq!(ServerOrigin::parse("https://jarvis.example.com/#f"), Err(OriginError::NotAnOrigin));
        assert_eq!(ServerOrigin::parse("https://u:p@jarvis.example.com"), Err(OriginError::Credentials)); // TEST-ONLY placeholder: a rejected credentialed URL
        assert_eq!(ServerOrigin::parse("file:///etc/passwd"), Err(OriginError::NotAUrl));
        assert_eq!(ServerOrigin::parse("javascript:alert(1)"), Err(OriginError::NotAUrl));
        assert_eq!(ServerOrigin::parse("not a url"), Err(OriginError::NotAUrl));
        // "localhost" only as the whole host, not as a prefix of another.
        assert_eq!(ServerOrigin::parse("http://localhost.evil.example"), Err(OriginError::Insecure));
    }

    #[test]
    fn api_urls_stay_on_the_origin() {
        let o = ServerOrigin::parse("https://jarvis.example.com").unwrap();
        assert_eq!(o.api("presentation/activity").as_str(), "https://jarvis.example.com/api/v1/presentation/activity");
        assert!(o.same_origin(&o.api("/sessions/token")));
        assert!(!o.same_origin(&Url::parse("https://evil.example/api/v1/").unwrap()));
        assert_eq!(o.as_str(), "https://jarvis.example.com");
    }

    #[test]
    fn web_urls() {
        assert!(validate_web_url("https://jarvis.example.com/ui/").is_ok());
        assert!(validate_web_url("http://jarvis.example.com/ui/").is_err());
        assert!(validate_web_url("https://user:pw@jarvis.example.com/").is_err()); // TEST-ONLY placeholder
        assert!(validate_web_url("file:///tmp/x.html").is_err());
    }
}
