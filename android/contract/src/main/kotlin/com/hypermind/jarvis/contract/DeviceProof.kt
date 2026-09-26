package com.hypermind.jarvis.contract

import java.security.MessageDigest
import java.util.UUID

/**
 * The signed messages a device produces with its Ed25519 key — byte-for-byte
 * the formats `server/auth/device.py` verifies (03 §4, docs/23 §3), pinned by
 * `shared/android/proof_vectors.json`.
 *
 * Only a [Signer] is needed: the private key never leaves whatever holds it
 * (the Android Keystore on a phone). Nothing here stores or sees key material.
 */
fun interface Signer {
    fun sign(message: ByteArray): ByteArray
}

object DeviceProof {
    const val VERSION = "v1"
    private const val PROOF_PREFIX = "hypermind-device-proof"
    private const val REGISTRATION_PREFIX = "hypermind-device-register"
    private const val ROTATION_PREFIX = "hypermind-device-rotate"
    private const val NONCE_BYTES = 16

    fun b64(raw: ByteArray): String = Base64Url.encode(raw)

    fun unb64(value: String): ByteArray = Base64Url.decode(value)

    /** A fresh proof of possession for token refresh and the channel hello. */
    fun proof(
        signer: Signer,
        deviceId: UUID,
        issuedAtEpochSeconds: Long,
        nonce: String = randomNonce(),
    ): String {
        val message = "$PROOF_PREFIX|$VERSION|$deviceId|$nonce|$issuedAtEpochSeconds".toByteArray(Charsets.UTF_8)
        return "$VERSION.$deviceId.$nonce.$issuedAtEpochSeconds.${b64(signer.sign(message))}"
    }

    /** Proof that this device holds the key it registers, bound to one bootstrap token. */
    fun registrationSignature(
        signer: Signer,
        bootstrapToken: String,
        publicKeyB64: String,
    ): String {
        val digest =
            MessageDigest
                .getInstance("SHA-256")
                .digest(bootstrapToken.toByteArray(Charsets.UTF_8))
                .joinToString("") { "%02x".format(it) }
        return b64(signer.sign("$REGISTRATION_PREFIX|$VERSION|$digest|$publicKeyB64".toByteArray(Charsets.UTF_8)))
    }

    /** Proof, signed by the *new* key, when rotating to it. */
    fun rotationSignature(
        signer: Signer,
        deviceId: UUID,
        publicKeyB64: String,
    ): String = b64(signer.sign("$ROTATION_PREFIX|$VERSION|$deviceId|$publicKeyB64".toByteArray(Charsets.UTF_8)))

    private fun randomNonce(): String {
        val bytes = ByteArray(NONCE_BYTES)
        java.security.SecureRandom().nextBytes(bytes)
        return b64(bytes)
    }
}
