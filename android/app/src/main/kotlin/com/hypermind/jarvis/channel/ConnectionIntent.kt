package com.hypermind.jarvis.channel

import android.content.SharedPreferences

/**
 * Whether the user wants this phone connected (docs/23 §4). Set by Connect,
 * cleared by Disconnect and by revocation. A push wake restarts the channel
 * only while this is set: a wake never overrides the user's own Disconnect.
 */
class ConnectionIntent(
    private val prefs: SharedPreferences,
) {
    @get:Synchronized @set:Synchronized
    var wanted: Boolean
        get() = prefs.getBoolean(WANTED, false)
        set(value) {
            prefs.edit().putBoolean(WANTED, value).commit()
        }

    private companion object {
        const val WANTED = "connection_wanted"
    }
}
