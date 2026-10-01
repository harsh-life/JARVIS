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

    /**
     * docs/29 §17.1: the agent an agent's reminder offers to run, recorded
     * from the frame this phone received — like the words, never from an
     * intent. Bounded and wiped with them.
     */
    fun rememberAgent(
        deliveryId: String,
        agentId: String,
    )

    fun agentFor(deliveryId: String): String?

    fun wipe()
}

/**
 * "Run agent" from a reminder (docs/29 §17.1): what the app offers the user —
 * built only from this phone's own record of an agent reminder it received.
 * Nothing runs until the user presses Run in the app; the run is then the
 * user's own request to the server, decided there like any run.
 */
class RunOffer private constructor(
    val deliveryId: String,
    val agentId: String,
    val text: String,
) {
    // The reminder's words are the owner's: never through toString.
    override fun toString(): String = "RunOffer(deliveryId=$deliveryId)"

    companion object {
        fun of(
            deliveryId: String,
            drafts: ReminderDrafts,
        ): RunOffer? {
            val agentId = drafts.agentFor(deliveryId) ?: return null
            val text = drafts.lookup(deliveryId) ?: return null
            return RunOffer(deliveryId, agentId, text)
        }
    }
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
        order.dropLast(kept.size).forEach {
            editor.remove(KEY_PREFIX + it)
            editor.remove(AGENT_PREFIX + it)
        }
        editor.putString(KEY_PREFIX + deliveryId, text)
        editor.putString(KEY_ORDER, kept.joinToString(SEPARATOR))
        editor.apply()
    }

    @Synchronized
    override fun lookup(deliveryId: String): String? = prefs.getString(KEY_PREFIX + deliveryId, null)

    @Synchronized
    override fun rememberAgent(
        deliveryId: String,
        agentId: String,
    ) {
        // Kept only while its words are (same bound, same order).
        if (lookup(deliveryId) == null) return
        prefs.edit().putString(AGENT_PREFIX + deliveryId, agentId).apply()
    }

    @Synchronized
    override fun agentFor(deliveryId: String): String? =
        prefs.getString(AGENT_PREFIX + deliveryId, null)?.takeIf { lookup(deliveryId) != null }

    @Synchronized
    override fun wipe() {
        val editor = prefs.edit()
        order().forEach {
            editor.remove(KEY_PREFIX + it)
            editor.remove(AGENT_PREFIX + it)
        }
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
        const val AGENT_PREFIX = "reminder_agent_"
        const val KEY_ORDER = "reminder_draft_order"
        const val SEPARATOR = ","
    }
}
