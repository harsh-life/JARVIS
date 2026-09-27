package com.hypermind.jarvis.overlay

import android.content.SharedPreferences
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * The floating status overlay's on/off choice (docs/23 §1 "floating
 * overlay"). Off by default: it appears only after the user turns it on and
 * grants "display over other apps".
 */
class OverlaySettings(
    private val prefs: SharedPreferences,
) {
    private val _enabled = MutableStateFlow(prefs.getBoolean(KEY, false))
    val enabled: StateFlow<Boolean> = _enabled.asStateFlow()

    @Synchronized
    fun set(on: Boolean) {
        prefs.edit().putBoolean(KEY, on).commit()
        _enabled.value = on
    }

    private companion object {
        const val KEY = "overlay_enabled"
    }
}

/** Whether JARVIS's own screen is in front (the overlay then steps aside). */
object AppForeground {
    private val _visibleCount = MutableStateFlow(0)
    val visibleCount: StateFlow<Int> = _visibleCount.asStateFlow()

    @Synchronized
    fun started() {
        _visibleCount.value += 1
    }

    @Synchronized
    fun stopped() {
        _visibleCount.value = (_visibleCount.value - 1).coerceAtLeast(0)
    }
}

/**
 * When the overlay is on screen — one rule, so the service never has to
 * decide ad hoc. Shown only if the user turned it on, the system allows
 * drawing over other apps, this phone is enrolled, and JARVIS's own screen is
 * not already in front (where the full task panel is).
 */
object OverlayPolicy {
    fun visible(
        enabled: Boolean,
        canDraw: Boolean,
        enrolled: Boolean,
        appInForeground: Boolean,
    ): Boolean = enabled && canDraw && enrolled && !appInForeground
}
