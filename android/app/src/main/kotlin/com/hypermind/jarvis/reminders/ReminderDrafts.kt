package com.hypermind.jarvis.reminders

import android.content.SharedPreferences

/**
 * The words of reminders this phone actually received, by delivery id — the
 * only source "Start task" reads from (docs/22 §2).
 *
 * `MainActivity` is exported (it is the launcher), so any installed app can
 * start it with extras. A notification therefore carries only a delivery id,
 * and the text comes from here: another app can at most point at a reminder the
 * user already has, never put its own words in the task box under "From your
 * reminder". Bounded, app-private, wiped on revocation.
 */
interface ReminderDrafts {
    fun remember(
        deliveryId: String,
        text: String,
    )

    fun lookup(deliveryId: String): String?

    fun wipe()
}

class SharedPrefsReminderDrafts(
    private val prefs: SharedPreferences,
    private val capacity: Int = 32,
) : ReminderDrafts {
    @Synchronized
    override fun remember(
        deliveryId: String,
        text: String,
    ) {
        val order = order().filter { it != deliveryId } + deliveryId
        val kept = order.takeLast(capacity)
        val editor = prefs.edit()
        order.dropLast(kept.size).forEach { editor.remove(KEY_PREFIX + it) }
        editor.putString(KEY_PREFIX + deliveryId, text)
        editor.putString(KEY_ORDER, kept.joinToString(SEPARATOR))
        editor.apply()
    }

    @Synchronized
    override fun lookup(deliveryId: String): String? = prefs.getString(KEY_PREFIX + deliveryId, null)

    @Synchronized
    override fun wipe() {
        val editor = prefs.edit()
        order().forEach { editor.remove(KEY_PREFIX + it) }
        editor.remove(KEY_ORDER).apply()
    }

    private fun order(): List<String> =
        prefs
            .getString(KEY_ORDER, null)
            ?.split(SEPARATOR)
            ?.filter {
                it.isNotEmpty()
            }.orEmpty()

    private companion object {
        const val KEY_PREFIX = "reminder_draft_"
        const val KEY_ORDER = "reminder_draft_order"
        const val SEPARATOR = ","
    }
}
