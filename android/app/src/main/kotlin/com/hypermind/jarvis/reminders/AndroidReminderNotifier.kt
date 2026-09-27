package com.hypermind.jarvis.reminders

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import com.hypermind.jarvis.MainActivity
import com.hypermind.jarvis.R
import com.hypermind.jarvis.contract.Reminder
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * The system notification for a reminder (docs/22 §2). Tapping it — or its
 * "Start task" action — opens JARVIS with the reminder's words in the task box.
 * The user reads, edits, and presses Send: that is an ordinary task from this
 * authenticated session, authorized like any other. Nothing is sent from the
 * notification itself.
 *
 * The words are the owner's private data, so on a locked screen only
 * "Reminder" is shown ([Notification.VISIBILITY_PRIVATE] with a public version).
 */
class AndroidReminderNotifier(
    private val context: Context,
) : ReminderNotifier {
    override fun show(reminder: Reminder) {
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        ensureChannel(manager)
        val request = reminder.deliveryId.hashCode()
        val open =
            PendingIntent.getActivity(
                context,
                request,
                draftIntent(context, reminder.deliveryId),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
            )
        val title = title(reminder)
        val public =
            builder()
                .setContentTitle(context.getString(R.string.reminder_title))
                .setSmallIcon(R.drawable.ic_launcher_foreground)
                .build()
        val notification =
            builder()
                .setContentTitle(title)
                .setContentText(reminder.taskReason)
                .setStyle(Notification.BigTextStyle().bigText(reminder.taskReason))
                .setSmallIcon(R.drawable.ic_launcher_foreground)
                .setContentIntent(open)
                .setAutoCancel(true)
                .setVisibility(Notification.VISIBILITY_PRIVATE)
                .setPublicVersion(public)
                .addAction(
                    Notification.Action
                        .Builder(null, context.getString(R.string.reminder_start_task), open)
                        .build(),
                ).build()
        // Tagged by delivery: a re-sent reminder replaces its own notification.
        manager.notify(reminder.deliveryId, NOTIFICATION_ID, notification)
    }

    private fun title(reminder: Reminder): String {
        if (!reminder.late) return context.getString(R.string.reminder_title)
        val at =
            try {
                OffsetDateTime
                    .parse(reminder.scheduledFor)
                    .atZoneSameInstant(ZoneId.systemDefault())
                    .format(DateTimeFormatter.ofPattern("HH:mm"))
            } catch (ignored: java.time.format.DateTimeParseException) {
                null
            }
        return if (at != null) {
            context.getString(R.string.reminder_title_missed_at, at)
        } else {
            context.getString(R.string.reminder_title_late)
        }
    }

    private fun builder(): Notification.Builder =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(context, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(context)
        }

    private fun ensureChannel(manager: NotificationManager) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_ID,
                    context.getString(R.string.reminder_channel_name),
                    NotificationManager.IMPORTANCE_DEFAULT,
                ).apply {
                    description = context.getString(R.string.reminder_channel_description)
                    lockscreenVisibility = Notification.VISIBILITY_PRIVATE
                },
            )
        }
    }

    companion object {
        const val CHANNEL_ID = "jarvis_reminders"
        private const val NOTIFICATION_ID = 4220

        /**
         * Opens the app with this reminder's words as a draft task — never
         * submitted by itself. Only the delivery id travels in the intent; the
         * words are read from [ReminderDrafts], never from the intent.
         */
        fun draftIntent(
            context: Context,
            deliveryId: String,
        ): Intent =
            Intent(context, MainActivity::class.java)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
                .putExtra(MainActivity.EXTRA_REMINDER_DELIVERY, deliveryId)
    }
}
