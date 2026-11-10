//! The device credential in the operating system's credential store
//! (OD-DESK-1): macOS Keychain, Windows Credential Manager, or the Linux
//! Secret Service (GNOME Keyring, KDE Wallet).
//!
//! What is stored: the device id, the server origin it belongs to, and the
//! Ed25519 seed. No access token is ever stored — tokens live in memory only
//! and are re-minted from the key. There is **no fallback**: if the store is
//! unavailable (no Secret Service on a Linux session, a locked keychain), sign
//! in fails with an explanation; the key is never written anywhere else.
//!
//! Honesty about protection: this is software key storage guarded by the OS
//! account's credential store. It is **not** hardware-bound — not Secure
//! Enclave, not TPM — and any process running as the same user may be able to
//! ask the store for it, subject to the OS's own prompts.

use serde::{Deserialize, Serialize};
use zeroize::{Zeroize, ZeroizeOnDrop, Zeroizing};

use crate::identity::{b64, b64_decode, DeviceKey};

pub const SERVICE: &str = "com.hypermind.jarvis.desktop";
const ACCOUNT: &str = "device-credential";
const PROBE_ACCOUNT: &str = "availability-probe";

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum StoreError {
    #[error("the operating system's credential store is not available ({0}). On Linux, JARVIS needs a Secret Service provider such as GNOME Keyring or KDE Wallet, unlocked in this session. Nothing was saved anywhere else.")]
    Unavailable(String),
    #[error("the stored device credential is damaged")]
    Corrupt,
}

/// What lives in the credential store.
#[derive(Serialize, Deserialize, Zeroize, ZeroizeOnDrop)]
pub struct StoredCredential {
    pub device_id: String,
    pub server: String,
    seed_b64: String,
}

impl StoredCredential {
    pub fn new(device_id: String, server: String, key: &DeviceKey) -> Self {
        StoredCredential { device_id, server, seed_b64: b64(key.seed().as_ref()) }
    }

    pub fn key(&self) -> Result<DeviceKey, StoreError> {
        let seed = Zeroizing::new(b64_decode(&self.seed_b64).ok_or(StoreError::Corrupt)?);
        DeviceKey::from_seed(&seed).ok_or(StoreError::Corrupt)
    }
}

impl std::fmt::Debug for StoredCredential {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("StoredCredential").field("device_id", &self.device_id).field("seed", &"<redacted>").finish()
    }
}

pub trait CredentialStore: Send + Sync {
    /// Write and remove a throwaway entry: proves the store works *before* a
    /// device is registered, so a failure never strands a registration.
    fn probe(&self) -> Result<(), StoreError>;
    fn load(&self) -> Result<Option<StoredCredential>, StoreError>;
    fn save(&self, credential: &StoredCredential) -> Result<(), StoreError>;
    fn delete(&self) -> Result<(), StoreError>;
}

/// The real OS store, through the `keyring` crate.
pub struct OsStore;

fn entry(account: &str) -> Result<keyring::Entry, StoreError> {
    keyring::Entry::new(SERVICE, account).map_err(|e| StoreError::Unavailable(kind(&e)))
}

fn kind(e: &keyring::Error) -> String {
    // The variant only: an error's text can quote attributes, never secrets,
    // but nothing here needs more than the kind.
    match e {
        keyring::Error::PlatformFailure(_) => "platform failure".into(),
        keyring::Error::NoStorageAccess(_) => "no storage access".into(),
        keyring::Error::NoEntry => "no entry".into(),
        keyring::Error::BadEncoding(_) => "bad encoding".into(),
        keyring::Error::TooLong(_, _) => "too long".into(),
        keyring::Error::Invalid(_, _) => "invalid".into(),
        keyring::Error::Ambiguous(_) => "ambiguous".into(),
        _ => "unknown".into(),
    }
}

impl CredentialStore for OsStore {
    fn probe(&self) -> Result<(), StoreError> {
        let e = entry(PROBE_ACCOUNT)?;
        e.set_password("probe").map_err(|err| StoreError::Unavailable(kind(&err)))?;
        let read = e.get_password().map_err(|err| StoreError::Unavailable(kind(&err)))?;
        let _ = e.delete_credential();
        if read != "probe" {
            return Err(StoreError::Unavailable("read-back mismatch".into()));
        }
        Ok(())
    }

    fn load(&self) -> Result<Option<StoredCredential>, StoreError> {
        match entry(ACCOUNT)?.get_password() {
            Ok(text) => {
                let text = Zeroizing::new(text);
                serde_json::from_str(&text).map(Some).map_err(|_| StoreError::Corrupt)
            }
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(e) => Err(StoreError::Unavailable(kind(&e))),
        }
    }

    fn save(&self, credential: &StoredCredential) -> Result<(), StoreError> {
        let text = Zeroizing::new(serde_json::to_string(credential).expect("serialisable"));
        entry(ACCOUNT)?.set_password(&text).map_err(|e| StoreError::Unavailable(kind(&e)))
    }

    fn delete(&self) -> Result<(), StoreError> {
        match entry(ACCOUNT)?.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(e) => Err(StoreError::Unavailable(kind(&e))),
        }
    }
}

/// An in-memory store for tests.
#[cfg(test)]
#[derive(Default)]
pub struct MemoryStore {
    pub value: std::sync::Mutex<Option<String>>,
    pub unavailable: bool,
}

#[cfg(test)]
impl CredentialStore for MemoryStore {
    fn probe(&self) -> Result<(), StoreError> {
        if self.unavailable {
            Err(StoreError::Unavailable("test".into()))
        } else {
            Ok(())
        }
    }
    fn load(&self) -> Result<Option<StoredCredential>, StoreError> {
        self.probe()?;
        Ok(self.value.lock().unwrap().as_ref().map(|t| serde_json::from_str(t).unwrap()))
    }
    fn save(&self, c: &StoredCredential) -> Result<(), StoreError> {
        self.probe()?;
        *self.value.lock().unwrap() = Some(serde_json::to_string(c).unwrap());
        Ok(())
    }
    fn delete(&self) -> Result<(), StoreError> {
        *self.value.lock().unwrap() = None;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_credential_round_trips_and_never_debug_prints_its_key() {
        let key = DeviceKey::generate();
        let c = StoredCredential::new("dev".into(), "https://j.example".into(), &key);
        assert_eq!(c.key().unwrap().public_b64(), key.public_b64());
        let shown = format!("{c:?}");
        assert!(!shown.contains(&b64(key.seed().as_ref())));
        assert!(shown.contains("redacted"));
    }

    #[test]
    fn an_unavailable_store_fails_and_stores_nothing() {
        let store = MemoryStore { unavailable: true, ..Default::default() };
        let c = StoredCredential::new("dev".into(), "s".into(), &DeviceKey::generate());
        assert!(matches!(store.save(&c), Err(StoreError::Unavailable(_))));
        assert!(store.value.lock().unwrap().is_none());
    }
}
