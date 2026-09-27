package com.hypermind.jarvis.contract

/**
 * The step-up re-attestation message (03 §5.5; docs/23 §3) — byte for byte
 * `server.auth.step_up.attestation_message`, held to the shared proof vectors.
 * Domain-separated from the device proof and bound to one device and one
 * single-use server challenge.
 */
object StepUp {
    const val PREFIX = "hypermind-step-up"
    const val VERSION = "v1"

    fun message(
        deviceId: String,
        challenge: String,
    ): ByteArray = "$PREFIX|$VERSION|$deviceId|$challenge".toByteArray(Charsets.UTF_8)
}
