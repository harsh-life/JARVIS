package com.hypermind.jarvis.push

import android.content.SharedPreferences
import com.hypermind.jarvis.contract.PushProviderKind
import java.security.MessageDigest

/**
 * This phone's push-wake settings (docs/23 §4). Off by default: `optedIn`
 * starts false, and nothing push-related runs until the user turns it on *and*
 * the server offers a provider. Only a hash of the last registered token is
 * kept, to avoid re-sending the same one — the token itself stays with the
 * push SDK.
 */
class PushSettings(
    private val prefs: SharedPreferences,
) {
    @get:Synchronized @set:Synchronized
    var optedIn: Boolean
        get() = prefs.getBoolean(OPTED_IN, false)
        set(value) {
            prefs.edit().putBoolean(OPTED_IN, value).commit()
        }

    /** What the server last said it offers (for the settings screen only). */
    @get:Synchronized @set:Synchronized
    var serverProvider: PushProviderKind
        get() = if (prefs.getString(SERVER_PROVIDER, null) == "fcm") PushProviderKind.FCM else PushProviderKind.NONE
        set(value) {
            prefs.edit().putString(SERVER_PROVIDER, if (value == PushProviderKind.FCM) "fcm" else "none").commit()
        }

    @Synchronized
    fun isRegistered(token: String): Boolean = prefs.getString(REGISTERED, null) == hash(token)

    @Synchronized
    fun hasRegistration(): Boolean = prefs.getString(REGISTERED, null) != null

    @Synchronized
    fun markRegistered(token: String?) {
        prefs.edit().apply { if (token == null) remove(REGISTERED) else putString(REGISTERED, hash(token)) }.commit()
    }

    /** Revocation or sign-out. */
    @Synchronized
    fun wipe() {
        prefs.edit().clear().commit()
    }

    private fun hash(token: String): String =
        MessageDigest
            .getInstance("SHA-256")
            .digest(token.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }

    private companion object {
        const val OPTED_IN = "opted_in"
        const val SERVER_PROVIDER = "server_provider"
        const val REGISTERED = "registered_token_sha256"
    }
}
