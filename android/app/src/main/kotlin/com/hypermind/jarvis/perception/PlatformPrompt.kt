package com.hypermind.jarvis.perception

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import android.provider.Settings
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.hypermind.jarvis.R
import com.hypermind.jarvis.contract.PlatformDependency

/**
 * docs/23 §5.3: when an operation is refused because an on-device dependency
 * is missing, the user is told plainly what to turn on — never a silent
 * failure. The notification names the dependency and nothing else: no task,
 * no operation, no screen content. Tapping it opens the place to fix it.
 */
class PlatformPrompt(
    private val context: Context,
) {
    fun show(dependency: PlatformDependency) {
        val manager = NotificationManagerCompat.from(context)
        if (!manager.areNotificationsEnabled()) return
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            context.getSystemService(NotificationManager::class.java).createNotificationChannel(
                NotificationChannel(
                    CHANNEL,
                    context.getString(R.string.platform_channel),
                    NotificationManager.IMPORTANCE_DEFAULT,
                ),
            )
        }
        val notification =
            NotificationCompat
                .Builder(context, CHANNEL)
                .setSmallIcon(android.R.drawable.stat_notify_error)
                .setContentTitle(context.getString(R.string.platform_title))
                .setContentText(context.getString(message(dependency)))
                .setStyle(NotificationCompat.BigTextStyle().bigText(context.getString(message(dependency))))
                .setAutoCancel(true)
                .apply { fix(dependency)?.let(::setContentIntent) }
                .build()
        try {
            manager.notify(dependency.ordinal + ID_BASE, notification)
        } catch (ignored: SecurityException) {
            // Notification permission withdrawn: the task's own status still says why.
        }
    }

    private fun message(dependency: PlatformDependency): Int =
        when (dependency) {
            PlatformDependency.SHIZUKU -> R.string.platform_shizuku
            PlatformDependency.ACCESSIBILITY_SERVICE -> R.string.platform_accessibility
            PlatformDependency.NOTIFICATION_ACCESS -> R.string.platform_notifications
            PlatformDependency.SCREEN_CAPTURE -> R.string.platform_screen_capture
            PlatformDependency.OCR -> R.string.platform_ocr
        }

    private fun fix(dependency: PlatformDependency): PendingIntent? {
        val intent =
            when (dependency) {
                PlatformDependency.SHIZUKU -> context.packageManager.getLaunchIntentForPackage(SHIZUKU_PACKAGE)
                PlatformDependency.ACCESSIBILITY_SERVICE, PlatformDependency.SCREEN_CAPTURE ->
                    Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)
                PlatformDependency.NOTIFICATION_ACCESS -> Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)
                PlatformDependency.OCR -> null
            } ?: return null
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return PendingIntent.getActivity(context, dependency.ordinal, intent, PendingIntent.FLAG_IMMUTABLE)
    }

    private companion object {
        const val CHANNEL = "platform"
        const val ID_BASE = 2000
        const val SHIZUKU_PACKAGE = "moe.shizuku.privileged.api"
    }
}
