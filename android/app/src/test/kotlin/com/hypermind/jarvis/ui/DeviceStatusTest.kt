package com.hypermind.jarvis.ui

import android.content.Context
import android.content.Intent
import android.provider.Settings
import androidx.test.core.app.ApplicationProvider
import com.hypermind.jarvis.contract.PlatformDependency
import com.hypermind.jarvis.presentation.ConnectionPhase
import com.hypermind.jarvis.presentation.DeviceContext
import com.hypermind.jarvis.presentation.PresentationText.UserAction
import com.hypermind.jarvis.presentation.PushWake
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

/** "This phone" and where its fix-it actions lead (navigation only). */
@RunWith(RobolectricTestRunner::class)
class DeviceStatusTest {
    private val context = ApplicationProvider.getApplicationContext<Context>()

    private fun rows(ctx: DeviceContext) = DeviceStatusRows.of(ctx).associateBy { it.label }

    @Test
    fun `connection, push and every dependency are shown in plain words`() {
        val ctx =
            DeviceContext(
                connection = ConnectionPhase.RECONNECTING,
                platforms =
                    mapOf(
                        PlatformDependency.ACCESSIBILITY_SERVICE to true,
                        PlatformDependency.NOTIFICATION_ACCESS to false,
                        PlatformDependency.SHIZUKU to false,
                    ),
                pushWake = PushWake.ON,
            )
        val shown = rows(ctx)
        assertEquals("Reconnecting", shown.getValue("Server connection").value)
        assertEquals("On", shown.getValue("Push wake").value)
        assertEquals("On", shown.getValue("Accessibility access").value)
        val notif = shown.getValue("Notification access")
        assertFalse(notif.ok)
        assertEquals(UserAction.OPEN_NOTIFICATION_ACCESS_SETTINGS, notif.action)
        assertEquals(UserAction.OPEN_SHIZUKU, shown.getValue("Shizuku (only for stopping apps)").action)
        assertEquals("Unknown", shown.getValue("Screenshots (visual fallback)").value)
    }

    @Test
    fun `offline offers connect, revoked offers signing in again, update required says so`() {
        assertEquals(
            UserAction.CONNECT,
            rows(DeviceContext(ConnectionPhase.OFFLINE)).getValue("Server connection").action,
        )
        assertEquals(
            UserAction.SIGN_IN_AGAIN,
            rows(DeviceContext(ConnectionPhase.REVOKED)).getValue("Server connection").action,
        )
        assertEquals(
            "Update the app",
            rows(DeviceContext(ConnectionPhase.UPDATE_REQUIRED)).getValue("Server connection").value,
        )
        assertEquals(
            "Not available on this phone",
            rows(DeviceContext(ConnectionPhase.CONNECTED, pushWake = PushWake.UNAVAILABLE)).getValue("Push wake").value,
        )
    }

    @Test
    fun `fix-it actions open system settings or an app, and nothing else`() {
        assertEquals(
            Settings.ACTION_ACCESSIBILITY_SETTINGS,
            UserActionIntents.of(context, UserAction.OPEN_ACCESSIBILITY_SETTINGS)?.action,
        )
        assertEquals(
            Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS,
            UserActionIntents.of(context, UserAction.OPEN_NOTIFICATION_ACCESS_SETTINGS)?.action,
        )
        // Shizuku not installed here: no intent (the app tells the user to install it).
        assertNull(UserActionIntents.of(context, UserAction.OPEN_SHIZUKU))
        for (other in listOf(
            UserAction.CONNECT,
            UserAction.TRY_AGAIN,
            UserAction.OPEN_APP_TO_APPROVE,
            UserAction.SIGN_IN_AGAIN,
        )) {
            assertNull(other.name, UserActionIntents.of(context, other))
        }
        val intent = UserActionIntents.of(context, UserAction.OPEN_ACCESSIBILITY_SETTINGS)!!
        assertTrue(intent.flags and Intent.FLAG_ACTIVITY_NEW_TASK != 0)
        assertTrue(intent.extras == null || intent.extras!!.isEmpty) // carries nothing
    }
}
