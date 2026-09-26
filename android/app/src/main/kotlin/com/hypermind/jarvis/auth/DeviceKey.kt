package com.hypermind.jarvis.auth

import com.hypermind.jarvis.contract.Signer

/**
 * The device's Ed25519 credential (03 §4, docs/23 §3). Only the public half
 * and a signing operation are exposed — the private key never leaves the
 * Keystore (or, where the Keystore cannot hold Ed25519, the Keystore-sealed
 * file only ever decrypted inside [sign]).
 */
interface DeviceKey : Signer {
    val publicKey: ByteArray
    val protection: KeyProtection
}

/** How the private key is protected on this device — reported, never assumed. */
enum class KeyProtection {
    /** Ed25519 generated inside the Keystore, backed by TEE/StrongBox (Android 13+). */
    KEYSTORE_HARDWARE,

    /** Ed25519 generated inside the Keystore, software-backed. */
    KEYSTORE_SOFTWARE,

    /** Ed25519 key sealed by a hardware-backed Keystore AES-256-GCM key. */
    SEALED_HARDWARE,

    /** Ed25519 key sealed by a software-backed Keystore AES-256-GCM key. */
    SEALED_SOFTWARE,
}

/** Creates, loads and destroys the one device credential. */
interface DeviceKeyStore {
    fun load(): DeviceKey?

    fun create(): DeviceKey

    fun destroy()
}
