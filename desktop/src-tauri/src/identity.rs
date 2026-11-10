//! The desktop device's identity material (OD-DESK-1): the PKCE pair and
//! state for the loopback login, the Ed25519 device key, and the two signed
//! messages the server verifies. The formats mirror `server/auth/device.py`
//! byte for byte and are held to `shared/android/proof_vectors.json`.
//!
//! The private key exists in exactly two places: this process's memory
//! (zeroized on drop) and the OS credential store (`keystore.rs`). It is never
//! written to a file, a log, a URL or the webview.

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use ed25519_dalek::{Signer, SigningKey};
use sha2::{Digest, Sha256};
use zeroize::Zeroizing;

const PROOF_VERSION: &str = "v1";
const PROOF_PREFIX: &str = "hypermind-device-proof";
const REGISTRATION_PREFIX: &str = "hypermind-device-register";

pub fn b64(raw: &[u8]) -> String {
    URL_SAFE_NO_PAD.encode(raw)
}

pub fn b64_decode(text: &str) -> Option<Vec<u8>> {
    URL_SAFE_NO_PAD.decode(text.trim_end_matches('=')).ok()
}

fn random<const N: usize>() -> Zeroizing<[u8; N]> {
    let mut out = Zeroizing::new([0u8; N]);
    getrandom::getrandom(out.as_mut()).expect("the OS random source is unavailable");
    out
}

/// RFC 7636: a 43-character verifier (32 random bytes) and its S256 challenge.
pub struct Pkce {
    pub verifier: Zeroizing<String>,
    pub challenge: String,
}

impl Pkce {
    pub fn generate() -> Self {
        let verifier = Zeroizing::new(b64(random::<32>().as_ref()));
        let challenge = s256(&verifier);
        Pkce { verifier, challenge }
    }
}

pub fn s256(verifier: &str) -> String {
    b64(&Sha256::digest(verifier.as_bytes()))
}

/// The companion's own login nonce, echoed to its loopback listener.
pub fn login_state() -> String {
    b64(random::<32>().as_ref())
}

/// The desktop's Ed25519 device key.
pub struct DeviceKey {
    signing: SigningKey,
}

impl DeviceKey {
    pub fn generate() -> Self {
        DeviceKey { signing: SigningKey::from_bytes(&random::<32>()) }
    }

    pub fn from_seed(seed: &[u8]) -> Option<Self> {
        let bytes: [u8; 32] = seed.try_into().ok()?;
        let key = DeviceKey { signing: SigningKey::from_bytes(&bytes) };
        let mut bytes = bytes;
        zeroize::Zeroize::zeroize(&mut bytes);
        Some(key)
    }

    /// The 32-byte seed — only ever handed to the OS credential store.
    pub fn seed(&self) -> Zeroizing<[u8; 32]> {
        Zeroizing::new(self.signing.to_bytes())
    }

    pub fn public_b64(&self) -> String {
        b64(self.signing.verifying_key().as_bytes())
    }

    /// `key_proof` for `POST /devices`: proves this key is held, bound to the
    /// bootstrap token (by digest — the token itself is never signed in clear).
    pub fn registration_proof(&self, bootstrap_token: &str) -> String {
        b64(&self.signing.sign(&registration_message(bootstrap_token, &self.public_b64())).to_bytes())
    }

    /// The device proof `POST /sessions/token` accepts: a fresh nonce and the
    /// current time, signed. Single use on the server.
    pub fn device_proof(&self, device_id: &str, issued_at: i64) -> Zeroizing<String> {
        let nonce = b64(random::<16>().as_ref());
        self.device_proof_with_nonce(device_id, &nonce, issued_at)
    }

    pub fn device_proof_with_nonce(&self, device_id: &str, nonce: &str, issued_at: i64) -> Zeroizing<String> {
        let message = format!("{PROOF_PREFIX}|{PROOF_VERSION}|{device_id}|{nonce}|{issued_at}");
        let signature = self.signing.sign(message.as_bytes());
        Zeroizing::new(format!("{PROOF_VERSION}.{device_id}.{nonce}.{issued_at}.{}", b64(&signature.to_bytes())))
    }
}

pub fn registration_message(bootstrap_token: &str, public_key: &str) -> Vec<u8> {
    let digest = Sha256::digest(bootstrap_token.as_bytes());
    let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
    format!("{REGISTRATION_PREFIX}|{PROOF_VERSION}|{hex}|{public_key}").into_bytes()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn vectors() -> Value {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../../shared/android/proof_vectors.json");
        serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
    }

    #[test]
    fn rfc7636_s256() {
        assert_eq!(s256("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"), "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM");
    }

    #[test]
    fn pkce_has_the_server_shape() {
        let p = Pkce::generate();
        assert_eq!(p.verifier.len(), 43);
        assert_eq!(p.challenge.len(), 43);
        assert_eq!(p.challenge, s256(&p.verifier));
        assert_ne!(Pkce::generate().verifier.as_str(), p.verifier.as_str());
        assert!(login_state().len() >= 32);
    }

    #[test]
    fn the_device_formats_match_the_server_vectors() {
        let v = vectors();
        let seed = b64_decode(v["seed_b64"].as_str().unwrap()).unwrap();
        let key = DeviceKey::from_seed(&seed).unwrap();
        assert_eq!(key.public_b64(), v["public_key_b64"].as_str().unwrap());
        assert_eq!(
            key.registration_proof(v["bootstrap_token"].as_str().unwrap()),
            v["registration_signature"].as_str().unwrap()
        );
        let proof = key.device_proof_with_nonce(
            v["device_id"].as_str().unwrap(),
            v["nonce"].as_str().unwrap(),
            v["issued_at"].as_i64().unwrap(),
        );
        assert_eq!(proof.as_str(), v["proof"].as_str().unwrap());
    }

    #[test]
    fn a_seed_round_trips_and_proofs_are_fresh() {
        let key = DeviceKey::generate();
        let again = DeviceKey::from_seed(key.seed().as_ref()).unwrap();
        assert_eq!(key.public_b64(), again.public_b64());
        let a = key.device_proof("d", 1);
        let b = key.device_proof("d", 1);
        assert_ne!(a.as_str(), b.as_str(), "every proof carries a fresh nonce");
        assert!(DeviceKey::from_seed(&[0u8; 31]).is_none());
    }
}
