//! The desktop's JARVIS account: sign-in, the device credential, short-lived
//! access tokens, the activity request, and sign-out (OD-DESK-1).
//!
//! Everything secret stays here, in Rust: the PKCE verifier, the handoff code,
//! the bootstrap token, the device key and the access token. The webview never
//! receives any of them — `activity()` returns a validated report and nothing
//! else. Access tokens are held in memory only and re-minted from the device
//! key (the existing `POST /sessions/token` proof flow); there is no refresh
//! token to store. A `401` on minting means the device was revoked: the local
//! key is destroyed and the companion returns to signed-out.

use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use log::{info, warn};
use reqwest::Client;
use serde::Deserialize;
use serde_json::json;
use tokio::sync::Mutex;
use url::Url;
use zeroize::Zeroizing;

use crate::activity::ActivityReport;
use crate::identity::{login_state, DeviceKey, Pkce};
use crate::keystore::{CredentialStore, StoreError, StoredCredential};
use crate::loopback::{Loopback, LoopbackError};
use crate::net::{self, NetError, ServerOrigin};

/// Re-mint well before the server's 15-minute TTL; a `401` also triggers it.
const TOKEN_REFRESH_AFTER: Duration = Duration::from_secs(10 * 60);
pub const LOGIN_TIMEOUT: Duration = Duration::from_secs(10 * 60);

#[derive(Debug, thiserror::Error)]
pub enum AccountError {
    #[error("not signed in")]
    NotSignedIn,
    #[error("this desktop was signed out or revoked; sign in again")]
    Revoked,
    #[error("{0}")]
    Net(#[from] NetError),
    #[error("{0}")]
    Store(#[from] StoreError),
    #[error("{0}")]
    Loopback(#[from] LoopbackError),
    #[error("the server's answer was not what JARVIS expects ({0})")]
    Protocol(&'static str),
    #[error("the browser could not be opened")]
    Browser,
    #[error("a sign-in is already in progress")]
    Busy,
}

impl From<reqwest::Error> for AccountError {
    fn from(e: reqwest::Error) -> Self {
        AccountError::Net(e.into())
    }
}

struct Device {
    id: String,
    key: DeviceKey,
}

struct Token {
    value: Zeroizing<String>,
    minted: Instant,
}

#[derive(Default)]
struct Inner {
    origin: Option<ServerOrigin>,
    client: Option<Client>,
    device: Option<Device>,
    token: Option<Token>,
}

pub struct Account {
    store: Arc<dyn CredentialStore>,
    inner: Mutex<Inner>,
    signing_in: std::sync::atomic::AtomicBool,
}

#[derive(Deserialize)]
struct StartResponse {
    redirect_url: String,
}

#[derive(Deserialize)]
struct BootstrapResponse {
    bootstrap_token: Zeroizing<String>,
}

#[derive(Deserialize)]
struct RegisterResponse {
    device_id: uuid::Uuid,
    #[serde(default)]
    device_credential: Option<serde::de::IgnoredAny>,
}

#[derive(Deserialize)]
struct TokenResponse {
    access_token: Zeroizing<String>,
}

fn now_unix() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}

async fn json<T: for<'de> Deserialize<'de>>(resp: reqwest::Response) -> Result<T, AccountError> {
    let body = Zeroizing::new(net::read_body(resp).await?);
    serde_json::from_slice(&body).map_err(|_| AccountError::Protocol("malformed response"))
}

impl Account {
    pub fn new(store: Arc<dyn CredentialStore>) -> Self {
        Account { store, inner: Mutex::new(Inner::default()), signing_in: Default::default() }
    }

    /// Load this server's device credential from the OS store, if there is one.
    pub async fn restore(&self, origin: ServerOrigin) -> Result<bool, AccountError> {
        let mut inner = self.inner.lock().await;
        inner.client = Some(net::client(&origin));
        inner.token = None;
        inner.device = None;
        let found = self.store.load()?;
        if let Some(c) = found {
            if c.server == origin.as_str() {
                inner.device = Some(Device { id: c.device_id.clone(), key: c.key()? });
            } else {
                info!("the stored device credential belongs to another server; not used");
            }
        }
        inner.origin = Some(origin);
        Ok(inner.device.is_some())
    }

    pub async fn signed_in(&self) -> bool {
        self.inner.lock().await.device.is_some()
    }

    pub async fn origin(&self) -> Option<ServerOrigin> {
        self.inner.lock().await.origin.clone()
    }

    /// The whole sign-in: loopback + system browser + handoff + registration.
    /// `open_browser` receives the identity provider's URL (no credential in it).
    pub async fn sign_in<F>(&self, open_browser: F, timeout: Duration) -> Result<(), AccountError>
    where
        F: FnOnce(&Url) -> Result<(), ()>,
    {
        use std::sync::atomic::Ordering;
        if self.signing_in.swap(true, Ordering::SeqCst) {
            return Err(AccountError::Busy);
        }
        let result = self.sign_in_inner(open_browser, timeout).await;
        self.signing_in.store(false, Ordering::SeqCst);
        match &result {
            Ok(()) => info!("desktop device signed in"),
            Err(e) => warn!("sign-in failed: {e}"),
        }
        result
    }

    async fn sign_in_inner<F>(&self, open_browser: F, timeout: Duration) -> Result<(), AccountError>
    where
        F: FnOnce(&Url) -> Result<(), ()>,
    {
        let (origin, client) = {
            let inner = self.inner.lock().await;
            (
                inner.origin.clone().ok_or(AccountError::NotSignedIn)?,
                inner.client.clone().ok_or(AccountError::NotSignedIn)?,
            )
        };
        // Fail before anything is registered if the key could not be kept.
        self.store.probe()?;

        let pkce = Pkce::generate();
        let state = login_state();
        let loopback = Loopback::bind().await?;
        let start_url = {
            let mut u = origin.api("auth/oidc/start");
            u.query_pairs_mut()
                .append_pair("desktop_state", &state)
                .append_pair("desktop_challenge", &pkce.challenge)
                .append_pair("desktop_port", &loopback.port.to_string());
            u
        };
        let start: StartResponse = json(client.get(start_url).send().await?).await?;
        let provider = Url::parse(&start.redirect_url).map_err(|_| AccountError::Protocol("redirect_url"))?;
        // The identity provider's page: https, or the same loopback dev server.
        if !(provider.scheme() == "https" || (!origin.is_https() && origin.same_origin(&provider))) {
            return Err(AccountError::Protocol("insecure identity provider URL"));
        }
        open_browser(&provider).map_err(|_| AccountError::Browser)?;

        let code = loopback.wait(&state, timeout).await?;
        let redeemed: BootstrapResponse = json(
            client
                .post(origin.api("auth/desktop/handoff"))
                .json(&json!({"code": code.as_str(), "code_verifier": pkce.verifier.as_str()}))
                .send()
                .await?,
        )
        .await?;

        let key = DeviceKey::generate();
        let registered: RegisterResponse = json(
            client
                .post(origin.api("devices"))
                .bearer_auth(redeemed.bootstrap_token.as_str())
                .json(&json!({
                    "platform": "desktop",
                    "public_key": key.public_b64(),
                    "key_proof": key.registration_proof(&redeemed.bootstrap_token),
                }))
                .send()
                .await?,
        )
        .await?;
        if registered.device_credential.is_some() {
            // The server must never mint a desktop's key (OD-DESK-1).
            return Err(AccountError::Protocol("server returned a device credential"));
        }
        let device_id = registered.device_id.to_string();

        let credential = StoredCredential::new(device_id.clone(), origin.as_str().to_string(), &key);
        if let Err(e) = self.store.save(&credential) {
            // Registered but not storable: revoke it rather than strand it.
            let device = Device { id: device_id, key };
            if let Ok(token) = mint(&client, &origin, &device).await {
                let _ = revoke(&client, &origin, &device, &token).await;
            }
            return Err(e.into());
        }

        let device = Device { id: device_id, key };
        let token = mint(&client, &origin, &device).await?;
        let mut inner = self.inner.lock().await;
        inner.device = Some(device);
        inner.token = Some(token);
        Ok(())
    }

    /// The caller's activity report, validated. Mints/re-mints the access
    /// token as needed; a revoked device signs this desktop out.
    pub async fn activity(&self) -> Result<ActivityReport, AccountError> {
        let mut inner = self.inner.lock().await;
        for attempt in 0..2 {
            let token = self.token(&mut inner).await?;
            let (client, origin) = (inner.client.clone().unwrap(), inner.origin.clone().unwrap());
            let resp = client.get(origin.api("presentation/activity")).bearer_auth(token.as_str()).send().await?;
            match net::read_body(resp).await {
                Ok(body) => {
                    return ActivityReport::parse(&body).ok_or(AccountError::Protocol("invalid activity report"))
                }
                Err(e) if e.is_unauthorized() && attempt == 0 => {
                    inner.token = None; // re-mint once; a revoked device fails there
                }
                Err(e) => return Err(e.into()),
            }
        }
        Err(AccountError::Revoked)
    }

    async fn token(&self, inner: &mut Inner) -> Result<Zeroizing<String>, AccountError> {
        if let Some(t) = &inner.token {
            if t.minted.elapsed() < TOKEN_REFRESH_AFTER {
                return Ok(t.value.clone());
            }
        }
        let (client, origin) = (
            inner.client.clone().ok_or(AccountError::NotSignedIn)?,
            inner.origin.clone().ok_or(AccountError::NotSignedIn)?,
        );
        let device = inner.device.as_ref().ok_or(AccountError::NotSignedIn)?;
        match mint(&client, &origin, device).await {
            Ok(t) => {
                let value = t.value.clone();
                inner.token = Some(t);
                Ok(value)
            }
            Err(AccountError::Net(e)) if e.is_unauthorized() => {
                warn!("the server no longer accepts this desktop's device credential; signing out locally");
                inner.device = None;
                inner.token = None;
                self.store.delete()?;
                Err(AccountError::Revoked)
            }
            Err(e) => Err(e),
        }
    }

    /// Sign out: revoke this desktop on the server when it can be reached, and
    /// always destroy the local key and token. Returns whether the server
    /// confirmed the revocation (false = offline: revoke it from another device).
    pub async fn sign_out(&self) -> Result<bool, AccountError> {
        let mut inner = self.inner.lock().await;
        let mut confirmed = false;
        let cached = inner.token.take();
        let device = inner.device.take();
        if let (Some(client), Some(origin), Some(device)) =
            (inner.client.clone(), inner.origin.clone(), device.as_ref())
        {
            let token = match cached {
                Some(t) => Ok(t),
                None => mint(&client, &origin, device).await,
            };
            if let Ok(token) = token {
                confirmed = revoke(&client, &origin, device, &token).await.is_ok();
            }
        }
        drop(device);
        drop(inner);
        self.store.delete()?;
        info!("desktop device signed out (server revocation {})", if confirmed { "confirmed" } else { "not reached" });
        Ok(confirmed)
    }
}

async fn mint(client: &Client, origin: &ServerOrigin, device: &Device) -> Result<Token, AccountError> {
    let proof = device.key.device_proof(&device.id, now_unix());
    let resp: TokenResponse = json(
        client.post(origin.api("sessions/token")).json(&json!({"device_credential": proof.as_str()})).send().await?,
    )
    .await?;
    Ok(Token { value: resp.access_token, minted: Instant::now() })
}

async fn revoke(client: &Client, origin: &ServerOrigin, device: &Device, token: &Token) -> Result<(), AccountError> {
    let resp =
        client.delete(origin.api(&format!("devices/{}", device.id))).bearer_auth(token.value.as_str()).send().await?;
    net::read_body(resp).await?;
    Ok(())
}
