//! End-to-end tests of the native core against a mock JARVIS server, and the
//! webview boundary (capabilities, commands, CSP).
//!
//! The mock speaks the real protocol (server/auth/desktop_handoff.py,
//! server/gateway/routers/auth.py): the test checks the PKCE verifier
//! against the challenge and the registration signature against the key,
//! as the server does.

use std::sync::Arc;
use std::time::Duration;

use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use serde_json::{json, Value};
use wiremock::matchers::{header, method, path};
use wiremock::{Mock, MockServer, Request, ResponseTemplate};

use crate::account::{Account, AccountError};
use crate::identity::{b64_decode, registration_message, s256};
use crate::keystore::{CredentialStore, MemoryStore};
use crate::net::ServerOrigin;
use crate::{logging, COMPANION_COMMANDS, SETTINGS_COMMANDS};

const HANDOFF: &str = "handoff-code-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
const BOOTSTRAP: &str = "bootstrap-token-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB";
const ACCESS: &str = "access-token-CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC";
const DEVICE_ID: &str = "6f1c2d3e-4a5b-4c6d-8e7f-0123456789ab";
const REPORT: &str = r#"{"schema_version":1,"computed_at":"2026-11-10T12:00:00Z","window_s":1800,"scores":{"happy":8,"satisfied":8,"default":5,"sleepy":1,"surprised":1,"angry":2,"sad":1},"components":{"engagement":61,"productivity":88,"steadiness":100,"failure":0,"pressure":8,"surprise":0,"inactivity":0}}"#;

fn query(req: &Request, key: &str) -> Option<String> {
    req.url.query_pairs().find(|(k, _)| k == key).map(|(_, v)| v.into_owned())
}

type Started = Arc<std::sync::Mutex<Option<(String, String)>>>;

async fn mock_jarvis(server: &MockServer) -> Started {
    let base = server.uri();
    let started: Started = Default::default();
    let capture = started.clone();
    Mock::given(method("GET"))
        .and(path("/api/v1/auth/oidc/start"))
        .respond_with(move |req: &Request| {
            *capture.lock().unwrap() =
                Some((query(req, "desktop_state").unwrap(), query(req, "desktop_port").unwrap()));
            ResponseTemplate::new(200).set_body_json(json!({"redirect_url": format!("{base}/fake-provider/authorize")}))
        })
        .mount(server)
        .await;
    Mock::given(method("POST"))
        .and(path("/api/v1/auth/desktop/handoff"))
        .respond_with(ResponseTemplate::new(200).set_body_json(json!({"bootstrap_token": BOOTSTRAP})))
        .mount(server)
        .await;
    Mock::given(method("POST"))
        .and(path("/api/v1/devices"))
        .and(header("authorization", format!("Bearer {BOOTSTRAP}").as_str()))
        .respond_with(ResponseTemplate::new(201).set_body_json(json!({"device_id": DEVICE_ID})))
        .mount(server)
        .await;
    Mock::given(method("POST"))
        .and(path("/api/v1/sessions/token"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(json!({"access_token": ACCESS, "expires_at": "2026-11-10T12:15:00Z"})),
        )
        .mount(server)
        .await;
    Mock::given(method("GET"))
        .and(path("/api/v1/presentation/activity"))
        .and(header("authorization", format!("Bearer {ACCESS}").as_str()))
        .respond_with(ResponseTemplate::new(200).set_body_string(REPORT))
        .mount(server)
        .await;
    Mock::given(method("DELETE"))
        .and(path(format!("/api/v1/devices/{DEVICE_ID}")))
        .respond_with(ResponseTemplate::new(204))
        .mount(server)
        .await;
    started
}

/// The browser: reads the start request's state and port and returns the
/// handoff to the loopback listener, as Google + the server's callback would.
fn browser(started: &Started, state_override: Option<String>) -> impl FnOnce(&url::Url) -> Result<(), ()> {
    let started = started.clone();
    move |provider: &url::Url| {
        assert!(provider.path().ends_with("/fake-provider/authorize"));
        let (state, port) = started.lock().unwrap().clone().unwrap();
        let state = state_override.unwrap_or(state);
        tokio::spawn(async move {
            let _ = reqwest::get(format!("http://127.0.0.1:{port}/jarvis/callback?code={HANDOFF}&state={state}")).await;
        });
        Ok(())
    }
}

async fn signed_in(server: &MockServer, started: &Started) -> (Account, Arc<MemoryStore>) {
    let store = Arc::new(MemoryStore::default());
    let account = Account::new(store.clone());
    account.restore(ServerOrigin::parse(&server.uri()).unwrap()).await.unwrap();
    account.sign_in(browser(started, None), Duration::from_secs(10)).await.unwrap();
    (account, store)
}

#[tokio::test]
async fn sign_in_speaks_the_protocol_and_leaks_nothing() {
    logging::capture();
    let server = MockServer::start().await;
    let started = mock_jarvis(&server).await;
    let (account, store) = signed_in(&server, &started).await;
    let report = account.activity().await.unwrap();
    assert_eq!(report.scores.happy, 8);

    let reqs = server.received_requests().await.unwrap();
    let start = reqs.iter().find(|r| r.url.path() == "/api/v1/auth/oidc/start").unwrap();
    let challenge = query(start, "desktop_challenge").unwrap();
    let port: u16 = query(start, "desktop_port").unwrap().parse().unwrap();
    assert!(port >= 1024);
    // The handoff is redeemed with the verifier whose S256 is the challenge.
    let handoff: Value =
        reqs.iter().find(|r| r.url.path() == "/api/v1/auth/desktop/handoff").unwrap().body_json().unwrap();
    assert_eq!(handoff["code"], HANDOFF);
    let verifier = handoff["code_verifier"].as_str().unwrap().to_string();
    assert_eq!(s256(&verifier), challenge);
    // The registration is a desktop with its own key, proven over the bootstrap token.
    let reg: Value = reqs.iter().find(|r| r.url.path() == "/api/v1/devices").unwrap().body_json().unwrap();
    assert_eq!(reg["platform"], "desktop");
    let public = reg["public_key"].as_str().unwrap();
    let key = VerifyingKey::from_bytes(&b64_decode(public).unwrap().try_into().unwrap()).unwrap();
    let sig = Signature::from_slice(&b64_decode(reg["key_proof"].as_str().unwrap()).unwrap()).unwrap();
    key.verify(&registration_message(BOOTSTRAP, public), &sig).unwrap();
    // No URL the client requested carries a credential.
    for r in &reqs {
        for secret in [HANDOFF, BOOTSTRAP, ACCESS, verifier.as_str()] {
            assert!(!r.url.as_str().contains(secret), "{} carried a secret", r.url);
        }
    }
    // The key went to the credential store, and only there.
    let stored = store.load().unwrap().unwrap();
    assert_eq!(stored.device_id, DEVICE_ID);
    assert_eq!(stored.key().unwrap().public_b64(), public);

    let seed = crate::identity::b64(stored.key().unwrap().seed().as_ref());
    let token_req: Value = reqs.iter().find(|r| r.url.path() == "/api/v1/sessions/token").unwrap().body_json().unwrap();
    let proof = token_req["device_credential"].as_str().unwrap().to_string();
    let logs = logging::captured().join("\n");
    assert!(!logs.is_empty());
    for secret in [HANDOFF, BOOTSTRAP, ACCESS, verifier.as_str(), seed.as_str(), proof.as_str(), REPORT, "\"happy\""] {
        assert!(!logs.contains(secret), "a log line carried a secret or report data");
    }
}

#[tokio::test]
async fn a_forged_return_to_the_listener_does_not_sign_in() {
    let server = MockServer::start().await;
    let started = mock_jarvis(&server).await;
    let store = Arc::new(MemoryStore::default());
    let account = Account::new(store.clone());
    account.restore(ServerOrigin::parse(&server.uri()).unwrap()).await.unwrap();
    let err = account.sign_in(browser(&started, Some("W".repeat(43))), Duration::from_millis(1500)).await.unwrap_err();
    assert!(matches!(err, AccountError::Loopback(_)), "{err}");
    assert!(store.load().unwrap().is_none());
    let reqs = server.received_requests().await.unwrap();
    assert!(reqs.iter().all(|r| r.url.path() != "/api/v1/auth/desktop/handoff"));
}

#[tokio::test]
async fn an_unavailable_credential_store_stops_sign_in_before_anything_is_sent() {
    let server = MockServer::start().await;
    let started = mock_jarvis(&server).await;
    let store = Arc::new(MemoryStore { unavailable: true, ..Default::default() });
    let account = Account::new(store);
    // restore() reads the store too, and fails the same way.
    assert!(account.restore(ServerOrigin::parse(&server.uri()).unwrap()).await.is_err());
    let err = account.sign_in(browser(&started, None), Duration::from_secs(2)).await.unwrap_err();
    assert!(matches!(err, AccountError::Store(_) | AccountError::NotSignedIn), "{err}");
    assert!(server.received_requests().await.unwrap().is_empty());
}

#[tokio::test]
async fn a_server_minted_desktop_key_is_refused() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/api/v1/devices"))
        .respond_with(
            ResponseTemplate::new(201).set_body_json(json!({"device_id": DEVICE_ID, "device_credential": "x"})),
        )
        .with_priority(1)
        .mount(&server)
        .await;
    let started = mock_jarvis(&server).await;
    let store = Arc::new(MemoryStore::default());
    let account = Account::new(store.clone());
    account.restore(ServerOrigin::parse(&server.uri()).unwrap()).await.unwrap();
    let err = account.sign_in(browser(&started, None), Duration::from_secs(10)).await.unwrap_err();
    assert!(matches!(err, AccountError::Protocol(_)), "{err}");
    assert!(store.load().unwrap().is_none());
}

#[tokio::test]
async fn a_revoked_device_signs_out_locally_and_never_reuses_a_token() {
    let server = MockServer::start().await;
    let started = mock_jarvis(&server).await;
    let (account, store) = signed_in(&server, &started).await;
    server.reset().await;
    // Every request now fails as for a revoked device.
    Mock::given(method("GET")).respond_with(ResponseTemplate::new(401)).mount(&server).await;
    Mock::given(method("POST")).respond_with(ResponseTemplate::new(401)).mount(&server).await;
    let err = account.activity().await.unwrap_err();
    assert!(matches!(err, AccountError::Revoked), "{err}");
    assert!(store.load().unwrap().is_none(), "the local key is destroyed");
    assert!(!account.signed_in().await);
    assert!(matches!(account.activity().await.unwrap_err(), AccountError::NotSignedIn));
}

#[tokio::test]
async fn an_expired_token_is_re_minted_once() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/api/v1/presentation/activity"))
        .respond_with(ResponseTemplate::new(401))
        .up_to_n_times(1)
        .with_priority(1)
        .mount(&server)
        .await;
    let started = mock_jarvis(&server).await;
    let (account, _) = signed_in(&server, &started).await;
    assert!(account.activity().await.is_ok());
    let mints =
        server.received_requests().await.unwrap().iter().filter(|r| r.url.path() == "/api/v1/sessions/token").count();
    assert_eq!(mints, 2, "minted at sign-in, and once more after the 401");
}

#[tokio::test]
async fn sign_out_revokes_on_the_server_when_it_can() {
    let server = MockServer::start().await;
    let started = mock_jarvis(&server).await;
    let (account, store) = signed_in(&server, &started).await;
    assert!(account.sign_out().await.unwrap());
    let reqs = server.received_requests().await.unwrap();
    let delete = reqs.iter().find(|r| r.method.as_str() == "DELETE").unwrap();
    assert_eq!(delete.url.path(), format!("/api/v1/devices/{DEVICE_ID}"));
    assert_eq!(delete.headers.get("authorization").unwrap(), &format!("Bearer {ACCESS}"));
    assert!(store.load().unwrap().is_none());
}

#[tokio::test]
async fn offline_sign_out_still_destroys_the_local_key() {
    let server = MockServer::start().await;
    let started = mock_jarvis(&server).await;
    let (account, store) = signed_in(&server, &started).await;
    // The server is down: nothing it answers succeeds.
    server.reset().await;
    Mock::given(wiremock::matchers::any()).respond_with(ResponseTemplate::new(503)).mount(&server).await;
    assert!(!account.sign_out().await.unwrap(), "not confirmed by the server");
    assert!(store.load().unwrap().is_none());
    assert!(!account.signed_in().await);
}

#[tokio::test]
async fn redirects_are_never_followed_so_no_credential_crosses_origins() {
    let elsewhere = MockServer::start().await;
    Mock::given(method("GET")).respond_with(ResponseTemplate::new(200).set_body_string(REPORT)).mount(&elsewhere).await;
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/api/v1/presentation/activity"))
        .respond_with(ResponseTemplate::new(302).insert_header("location", format!("{}/steal", elsewhere.uri())))
        .with_priority(1)
        .mount(&server)
        .await;
    let started = mock_jarvis(&server).await;
    let (account, _) = signed_in(&server, &started).await;
    assert!(account.activity().await.is_err());
    assert!(elsewhere.received_requests().await.unwrap().is_empty(), "the redirect target was never contacted");
}

#[tokio::test]
async fn an_invalid_report_is_refused_before_the_webview() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/api/v1/presentation/activity"))
        .respond_with(ResponseTemplate::new(200).set_body_string(REPORT.replace("\"happy\":8", "\"happy\":11")))
        .with_priority(1)
        .mount(&server)
        .await;
    let started = mock_jarvis(&server).await;
    let (account, _) = signed_in(&server, &started).await;
    assert!(matches!(account.activity().await.unwrap_err(), AccountError::Protocol(_)));
}

// ── the webview boundary ────────────────────────────────────────────────

fn read(rel: &str) -> String {
    std::fs::read_to_string(format!("{}/{rel}", env!("CARGO_MANIFEST_DIR"))).unwrap()
}

fn permissions(file: &str) -> Vec<String> {
    let v: Value = serde_json::from_str(&read(file)).unwrap();
    v["permissions"].as_array().unwrap().iter().map(|p| p.as_str().unwrap().to_string()).collect()
}

fn allow(commands: &[&str]) -> Vec<String> {
    commands.iter().map(|c| format!("allow-{}", c.replace('_', "-"))).collect()
}

#[test]
fn each_window_may_call_exactly_its_own_commands_and_nothing_else() {
    let events = vec!["core:event:allow-listen".to_string(), "core:event:allow-unlisten".to_string()];
    let mut companion = permissions("capabilities/companion.json");
    let mut expected = [events.clone(), allow(&COMPANION_COMMANDS)].concat();
    companion.sort();
    expected.sort();
    assert_eq!(companion, expected);

    let mut settings = permissions("capabilities/settings.json");
    let mut expected = [events, allow(&SETTINGS_COMMANDS)].concat();
    settings.sort();
    expected.sort();
    assert_eq!(settings, expected);

    let windows = |f: &str| -> Value { serde_json::from_str::<Value>(&read(f)).unwrap()["windows"].clone() };
    assert_eq!(windows("capabilities/companion.json"), json!(["companion"]));
    assert_eq!(windows("capabilities/settings.json"), json!(["settings"]));

    let all = std::fs::read_dir(format!("{}/capabilities", env!("CARGO_MANIFEST_DIR"))).unwrap().count();
    assert_eq!(all, 2, "no other capability file may exist");
    for p in permissions("capabilities/companion.json").iter().chain(permissions("capabilities/settings.json").iter()) {
        for forbidden in [
            "fs:",
            "http:",
            "shell:",
            "opener:",
            "clipboard",
            "process:",
            "core:default",
            "core:window",
            "core:webview",
            "dialog:",
        ] {
            assert!(!p.starts_with(forbidden) && !p.contains(forbidden), "{p}");
        }
    }
}

#[test]
fn the_registered_commands_are_exactly_the_manifest() {
    let build = read("build.rs");
    let lib = read("src/lib.rs");
    for c in COMPANION_COMMANDS.iter().chain(SETTINGS_COMMANDS.iter()) {
        assert!(build.contains(&format!("\"{c}\"")), "{c} missing from build.rs");
        assert!(lib.contains(&format!("fn {c}(")), "{c} has no handler");
    }
    let handler = lib.split("generate_handler![").nth(1).unwrap().split(']').next().unwrap();
    let mut registered: Vec<_> = handler.split(',').map(|s| s.trim()).filter(|s| !s.is_empty()).collect();
    let mut expected: Vec<_> = COMPANION_COMMANDS.iter().chain(SETTINGS_COMMANDS.iter()).copied().collect();
    registered.sort();
    expected.sort();
    assert_eq!(registered, expected);
}

#[test]
fn no_command_returns_or_accepts_a_credential() {
    let lib = read("src/lib.rs");
    for word in ["access_token", "device_credential", "bootstrap", "seed", "code_verifier", "private"] {
        for block in ["struct CompanionInit", "struct SettingsView", "struct SettingsPatch", "struct SignOutResult"] {
            let body = lib.split(block).nth(1).unwrap().split('}').next().unwrap();
            assert!(!body.contains(word), "{block} mentions {word}");
        }
    }
}

#[test]
fn the_webview_has_no_network_and_no_plugin_surface() {
    let conf: Value = serde_json::from_str(&read("tauri.conf.json")).unwrap();
    let csp = conf["app"]["security"]["csp"].as_str().unwrap();
    let connect = csp.split(';').find(|d| d.trim().starts_with("connect-src")).unwrap();
    assert_eq!(connect.trim(), "connect-src ipc: http://ipc.localhost");
    assert!(csp.contains("script-src 'self'") && !csp.contains("unsafe"));
    assert_eq!(conf["app"]["windows"], json!([]));
    assert!(conf["plugins"].is_null());
    let cargo = read("Cargo.toml");
    for plugin in [
        "tauri-plugin-http",
        "tauri-plugin-fs",
        "tauri-plugin-shell",
        "tauri-plugin-clipboard",
        "tauri-plugin-global-shortcut",
    ] {
        assert!(!cargo.contains(plugin), "{plugin}");
    }
}
