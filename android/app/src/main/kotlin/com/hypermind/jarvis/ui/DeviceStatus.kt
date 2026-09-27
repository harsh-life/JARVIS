package com.hypermind.jarvis.ui

import android.content.Context
import android.content.Intent
import android.provider.Settings
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.hypermind.jarvis.contract.PlatformDependency
import com.hypermind.jarvis.presentation.ConnectionPhase
import com.hypermind.jarvis.presentation.DeviceContext
import com.hypermind.jarvis.presentation.PresentationText
import com.hypermind.jarvis.presentation.PushWake

/**
 * "This phone" — the device's own state in plain words (docs/23 §7), from
 * the presentation's device context only. Deep diagnostics (reason strings,
 * codes) stay out of it.
 */
object DeviceStatusRows {
    data class Row(
        val label: String,
        val value: String,
        /** false: something the user may want to turn on or fix. */
        val ok: Boolean,
        val action: PresentationText.UserAction? = null,
    )

    fun of(context: DeviceContext): List<Row> =
        listOf(
            connection(context.connection),
            Row("Push wake", push(context.pushWake), context.pushWake != PushWake.UNAVAILABLE),
            platform(
                context,
                PlatformDependency.ACCESSIBILITY_SERVICE,
                "Accessibility access",
                PresentationText.UserAction.OPEN_ACCESSIBILITY_SETTINGS,
            ),
            platform(
                context,
                PlatformDependency.NOTIFICATION_ACCESS,
                "Notification access",
                PresentationText.UserAction.OPEN_NOTIFICATION_ACCESS_SETTINGS,
            ),
            platform(
                context,
                PlatformDependency.SHIZUKU,
                "Shizuku (only for stopping apps)",
                PresentationText.UserAction.OPEN_SHIZUKU,
            ),
            platform(context, PlatformDependency.SCREEN_CAPTURE, "Screenshots (visual fallback)", null),
        )

    private fun connection(phase: ConnectionPhase): Row =
        when (phase) {
            ConnectionPhase.CONNECTED -> Row("Server connection", "Connected", true)
            ConnectionPhase.CONNECTING -> Row("Server connection", "Connecting", true)
            ConnectionPhase.RECONNECTING -> Row("Server connection", "Reconnecting", false)
            ConnectionPhase.AUTH_EXPIRED -> Row("Server connection", "Signing in again", false)
            ConnectionPhase.OFFLINE ->
                Row("Server connection", "Not connected", false, PresentationText.UserAction.CONNECT)
            ConnectionPhase.CHANNEL_DISABLED -> Row("Server connection", "Turned off on your server", false)
            ConnectionPhase.UPDATE_REQUIRED ->
                Row("Server connection", "Update the app", false, PresentationText.UserAction.UPDATE_APP)
            ConnectionPhase.REVOKED ->
                Row("Server connection", "This phone was removed", false, PresentationText.UserAction.SIGN_IN_AGAIN)
            ConnectionPhase.NOT_ENROLLED -> Row("Server connection", "Not signed in", false)
        }

    private fun push(push: PushWake): String =
        when (push) {
            PushWake.NOT_OFFERED -> "Not offered by your server"
            PushWake.OFF -> "Off"
            PushWake.ON -> "On"
            PushWake.UNAVAILABLE -> "Not available on this phone"
        }

    private fun platform(
        context: DeviceContext,
        dependency: PlatformDependency,
        label: String,
        action: PresentationText.UserAction?,
    ): Row =
        when (context.platforms[dependency]) {
            true -> Row(label, "On", true)
            false -> Row(label, "Off", false, action)
            null -> Row(label, "Unknown", true)
        }
}

/** Where each suggested action leads: a system settings screen or an app. Never an approval or a grant. */
object UserActionIntents {
    const val SHIZUKU_PACKAGE = "moe.shizuku.privileged.api"

    fun of(
        context: Context,
        action: PresentationText.UserAction,
    ): Intent? =
        when (action) {
            PresentationText.UserAction.OPEN_ACCESSIBILITY_SETTINGS -> Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)
            PresentationText.UserAction.OPEN_NOTIFICATION_ACCESS_SETTINGS ->
                Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)
            PresentationText.UserAction.OPEN_SHIZUKU ->
                context.packageManager.getLaunchIntentForPackage(
                    SHIZUKU_PACKAGE,
                )
            else -> null
        }?.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
}

@Composable
fun DeviceStatusPanel(
    context: DeviceContext,
    onAction: (PresentationText.UserAction) -> Unit,
) {
    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        DeviceStatusRows.of(context).forEach { row ->
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text(row.label, style = MaterialTheme.typography.bodyMedium)
                Text(
                    row.value,
                    style = MaterialTheme.typography.bodyMedium,
                    color = if (row.ok) MaterialTheme.colorScheme.onSurface else MaterialTheme.colorScheme.error,
                )
            }
            row.action?.let { action ->
                androidx.compose.material3.OutlinedButton(onClick = { onAction(action) }) { Text(actionLabel(action)) }
            }
        }
    }
}
