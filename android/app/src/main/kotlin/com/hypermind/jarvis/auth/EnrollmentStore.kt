package com.hypermind.jarvis.auth

import android.content.SharedPreferences
import java.util.UUID

/**
 * What the app remembers between runs — and nothing secret. The private key
 * is in the Keystore ([DeviceKeyStore]); access tokens live only in memory
 * ([SessionManager]). Backups are excluded for the whole app
 * (data_extraction_rules.xml), so none of this is restored onto another phone.
 */
class EnrollmentStore(
    private val prefs: SharedPreferences,
) {
    var serverUrl: String?
        get() = prefs.getString(KEY_SERVER, null)
        set(value) = prefs.edit().putString(KEY_SERVER, value).apply()

    val deviceId: UUID?
        get() = prefs.getString(KEY_DEVICE, null)?.let(UUID::fromString)

    fun enrolled(deviceId: UUID) = prefs.edit().putString(KEY_DEVICE, deviceId.toString()).apply()

    /** The login nonce for an App Link return (docs/23 §3), with when it was made. */
    fun rememberLogin(appState: String, createdAtMillis: Long) =
        prefs
            .edit()
            .putString(KEY_APP_STATE, appState)
            .putLong(KEY_APP_STATE_AT, createdAtMillis)
            .apply()

    /** Single use: reading the pending login also forgets it. */
    fun takeLogin(): Pair<String, Long>? {
        val state = prefs.getString(KEY_APP_STATE, null) ?: return null
        val at = prefs.getLong(KEY_APP_STATE_AT, 0)
        prefs
            .edit()
            .remove(KEY_APP_STATE)
            .remove(KEY_APP_STATE_AT)
            .apply()
        return state to at
    }

    /** Forget the enrollment (revocation, sign-out). The server URL stays. */
    fun clearEnrollment() =
        prefs
            .edit()
            .remove(KEY_DEVICE)
            .remove(KEY_APP_STATE)
            .remove(KEY_APP_STATE_AT)
            .apply()

    private companion object {
        const val KEY_SERVER = "server_url"
        const val KEY_DEVICE = "device_id"
        const val KEY_APP_STATE = "pending_app_state"
        const val KEY_APP_STATE_AT = "pending_app_state_at"
    }
}
