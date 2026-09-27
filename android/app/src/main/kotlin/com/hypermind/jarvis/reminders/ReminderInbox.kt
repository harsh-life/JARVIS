package com.hypermind.jarvis.reminders

import com.hypermind.jarvis.contract.Reminder
import com.hypermind.jarvis.contract.ReminderAck

/** Shows a reminder to the user. It never starts anything by itself. */
fun interface ReminderNotifier {
    fun show(reminder: Reminder)
}

/**
 * Reminders arriving on the device channel (docs/22 §2/§3).
 *
 * * A reminder addressed to another device is neither shown nor acknowledged
 *   (the server sends only to this socket's own device; this is the device's
 *   own check, as for operations).
 * * The server re-sends until acknowledged, so the same delivery can arrive
 *   more than once; it is shown once and acknowledged every time.
 * * Nothing here can run a task, answer a confirmation or touch the device —
 *   the only output is a notification and an acknowledgement.
 */
class ReminderInbox(
    private val deviceId: () -> String?,
    private val notifier: ReminderNotifier,
    private val drafts: ReminderDrafts? = null,
    private val capacity: Int = 256,
) {
    private val seen = LinkedHashSet<String>()

    @Synchronized
    fun receive(reminder: Reminder): ReminderAck? {
        val me = deviceId() ?: return null
        if (reminder.deviceId != me) return null
        if (seen.add(reminder.deliveryId)) {
            if (seen.size > capacity) seen.remove(seen.first())
            // Recorded before it is shown: "Start task" reads the words from here.
            drafts?.remember(reminder.deliveryId, reminder.taskReason)
            notifier.show(reminder)
        }
        return ReminderAck(deliveryId = reminder.deliveryId)
    }
}
