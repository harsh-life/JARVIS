package com.hypermind.jarvis.permissions

import android.content.SharedPreferences
import com.hypermind.jarvis.contract.GridState
import com.hypermind.jarvis.contract.GridToggle

/**
 * The per-app capability grid on this device (PRD §13, docs/23 §5.2) — the
 * user's local refusal control. Every toggle starts OFF; turning one off takes
 * effect on the very next operation, whatever the server sent. It can never
 * grant anything: an operation still needs the server's own authorization
 * before it is ever sent here.
 */
class GridStore(
    private val prefs: SharedPreferences,
) {
    @Synchronized
    fun state(): GridState =
        GridState(
            packages =
                prefs.all.keys
                    .filter { it.startsWith(PACKAGE_PREFIX) }
                    .associate { key ->
                        key.removePrefix(PACKAGE_PREFIX) to
                            prefs
                                .getStringSet(key, emptySet())
                                .orEmpty()
                                .mapNotNull(::toggleOf)
                                .toSet()
                    },
            deviceState = prefs.getBoolean(DEVICE_STATE, false),
        )

    @Synchronized
    fun set(
        packageName: String,
        toggle: GridToggle,
        on: Boolean,
    ) {
        require(toggle != GridToggle.DEVICE_STATE) { "device state is not per app" }
        val key = PACKAGE_PREFIX + packageName
        val current = prefs.getStringSet(key, emptySet()).orEmpty().toMutableSet()
        if (on) current += toggle.name else current -= toggle.name
        prefs.edit().putStringSet(key, current).commit()
    }

    @Synchronized
    fun setDeviceState(on: Boolean) {
        prefs.edit().putBoolean(DEVICE_STATE, on).commit()
    }

    /** Revocation or sign-out: every toggle back to OFF. */
    @Synchronized
    fun wipe() {
        prefs.edit().clear().commit()
    }

    private fun toggleOf(name: String): GridToggle? = GridToggle.entries.firstOrNull { it.name == name }

    private companion object {
        const val PACKAGE_PREFIX = "pkg:"
        const val DEVICE_STATE = "device_state"
    }
}
