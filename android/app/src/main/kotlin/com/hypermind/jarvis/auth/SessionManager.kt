package com.hypermind.jarvis.auth

import com.hypermind.jarvis.contract.DeviceProof
import java.time.Instant
import java.time.OffsetDateTime
import java.util.UUID

/** The device's enrollment is gone (revoked, rotated away, user suspended): sign in again. */
class EnrollmentLost : Exception("sign in again")

/**
 * Short-lived access tokens (03 §5), kept in memory only and re-minted with a
 * fresh device proof when they near expiry. A refusal of the proof itself
 * (401) means the credential no longer works — [EnrollmentLost], never a retry
 * loop or a silent failure (03 §5.4, FAIL-013).
 */
class SessionManager(
    private val api: () -> ApiClient,
    private val key: () -> DeviceKey?,
    private val deviceId: () -> UUID?,
    private val now: () -> Instant = Instant::now,
) {
    private var cached: Pair<String, Instant>? = null

    @Synchronized
    fun accessToken(forceRefresh: Boolean = false): Pair<String, Instant> {
        val current = cached
        if (!forceRefresh && current != null && current.second.isAfter(now().plusSeconds(REFRESH_MARGIN_SECONDS))) {
            return current
        }
        val signer = key() ?: throw EnrollmentLost()
        val device = deviceId() ?: throw EnrollmentLost()
        val proof = DeviceProof.proof(signer, device, now().epochSecond)
        val token =
            try {
                api().token(proof)
            } catch (e: ApiException) {
                if (e.status == HTTP_UNAUTHORIZED) {
                    cached = null
                    throw EnrollmentLost()
                }
                throw e
            }
        val fresh = token.accessToken to OffsetDateTime.parse(token.expiresAt).toInstant()
        cached = fresh
        return fresh
    }

    /** A fresh device proof for the channel's hello (single-use nonce each time). */
    fun freshProof(): String {
        val signer = key() ?: throw EnrollmentLost()
        val device = deviceId() ?: throw EnrollmentLost()
        return DeviceProof.proof(signer, device, now().epochSecond)
    }

    @Synchronized
    fun forget() {
        cached = null
    }

    private companion object {
        const val REFRESH_MARGIN_SECONDS = 60L
        const val HTTP_UNAUTHORIZED = 401
    }
}
