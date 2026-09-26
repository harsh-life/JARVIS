package com.hypermind.jarvis.auth

import android.net.Uri
import com.hypermind.jarvis.contract.DeviceProof
import java.security.SecureRandom
import java.util.UUID

/** Why a returned login was not accepted. */
enum class LoginRejection { NO_PENDING_LOGIN, EXPIRED, STATE_MISMATCH, MALFORMED_LINK }

sealed interface LoginOutcome {
    data class Enrolled(
        val deviceId: UUID,
        val protection: KeyProtection,
    ) : LoginOutcome

    data class Rejected(
        val reason: LoginRejection,
    ) : LoginOutcome
}

/**
 * docs/23 §3's login handoff, device side.
 *
 * 1. [begin] makes a random login nonce, remembers it, and asks the server to
 *    start Google login bound to it.
 * 2. The browser returns through the verified App Link with the one-time
 *    bootstrap token and the nonce in the URL fragment. [complete] accepts it
 *    only if the nonce is the one this app made, unused, and recent — so a
 *    link from a login someone else started cannot enroll this phone to their
 *    account.
 * 3. A key pair is generated in the Keystore, and only its public half is
 *    registered, with a signature proving possession (03 §4.2).
 *
 * The device never tells the server who the user is: the account is whatever
 * the server derived from Google's verified identity (PHONE-003).
 */
class LoginCoordinator(
    private val store: EnrollmentStore,
    private val keys: DeviceKeyStore,
    private val api: () -> ApiClient,
    private val sessions: SessionManager,
    private val nowMillis: () -> Long = System::currentTimeMillis,
    private val random: SecureRandom = SecureRandom(),
) {
    /** Returns the Google sign-in URL to open in the browser. */
    fun begin(): String {
        val appState = DeviceProof.b64(ByteArray(NONCE_BYTES).also(random::nextBytes))
        store.rememberLogin(appState, nowMillis())
        return api().oidcStartUrl(appState)
    }

    /** Handle the App Link. Blocking (network); call off the main thread. */
    fun complete(link: Uri): LoginOutcome {
        val fragment =
            parseFragment(link.encodedFragment) ?: return LoginOutcome.Rejected(LoginRejection.MALFORMED_LINK)
        val bootstrap = fragment["bootstrap_token"]
        val returnedState = fragment["app_state"]
        val pending = store.takeLogin() ?: return LoginOutcome.Rejected(LoginRejection.NO_PENDING_LOGIN)
        if (bootstrap.isNullOrEmpty() || returnedState.isNullOrEmpty()) {
            return LoginOutcome.Rejected(LoginRejection.MALFORMED_LINK)
        }
        if (nowMillis() - pending.second > PENDING_LOGIN_MAX_AGE_MILLIS) {
            return LoginOutcome.Rejected(LoginRejection.EXPIRED)
        }
        if (!constantTimeEquals(pending.first, returnedState)) {
            return LoginOutcome.Rejected(LoginRejection.STATE_MISMATCH)
        }
        val key = keys.create()
        val publicKey = DeviceProof.b64(key.publicKey)
        val deviceId =
            api().register(
                bootstrapToken = bootstrap,
                publicKey = publicKey,
                keyProof = DeviceProof.registrationSignature(key, bootstrap, publicKey),
            )
        store.enrolled(deviceId)
        sessions.forget()
        sessions.accessToken(forceRefresh = true)
        return LoginOutcome.Enrolled(deviceId, key.protection)
    }

    private fun parseFragment(fragment: String?): Map<String, String>? {
        if (fragment.isNullOrEmpty() || fragment.length > MAX_FRAGMENT) return null
        return fragment
            .split('&')
            .mapNotNull { part ->
                val eq = part.indexOf('=')
                if (eq <= 0) null else Uri.decode(part.substring(0, eq)) to Uri.decode(part.substring(eq + 1))
            }.toMap()
    }

    private fun constantTimeEquals(
        a: String,
        b: String,
    ): Boolean = java.security.MessageDigest.isEqual(a.toByteArray(), b.toByteArray())

    private companion object {
        const val NONCE_BYTES = 32
        const val MAX_FRAGMENT = 2048
        const val PENDING_LOGIN_MAX_AGE_MILLIS = 10 * 60 * 1000L
    }
}
