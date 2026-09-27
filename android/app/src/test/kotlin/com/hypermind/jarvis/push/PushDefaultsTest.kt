package com.hypermind.jarvis.push

import android.content.ComponentName
import android.content.Context
import android.content.pm.PackageManager
import androidx.test.core.app.ApplicationProvider
import com.google.firebase.FirebaseApp
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

/**
 * docs/23 §4: push wake is OFF by default at the component level — as the
 * merged manifest ships, nothing on the phone can receive a push, and
 * starting the app never initializes Firebase.
 */
@RunWith(RobolectricTestRunner::class)
class PushDefaultsTest {
    private val context = ApplicationProvider.getApplicationContext<Context>()
    private val pm = context.packageManager

    private fun serviceEnabled(name: String) =
        pm.getServiceInfo(ComponentName(context, name), PackageManager.MATCH_DISABLED_COMPONENTS).enabled

    private fun receiverEnabled(name: String) =
        pm.getReceiverInfo(ComponentName(context, name), PackageManager.MATCH_DISABLED_COMPONENTS).enabled

    @Test
    fun `nothing can receive a push until the user turns push wake on`() {
        assertFalse(serviceEnabled(JarvisMessagingService::class.java.name))
        assertFalse(serviceEnabled("com.google.firebase.messaging.FirebaseMessagingService"))
        assertFalse(receiverEnabled("com.google.firebase.iid.FirebaseInstanceIdReceiver"))
    }

    @Test
    fun `firebase is never initialized automatically and never auto-registers`() {
        // The application (and its object graph) has started: no Firebase app.
        assertTrue(FirebaseApp.getApps(context).isEmpty())
        val providers =
            pm
                .getPackageInfo(context.packageName, PackageManager.GET_PROVIDERS)
                .providers
                .orEmpty()
                .map { it.name }
        assertFalse(providers.any { it.contains("FirebaseInitProvider") })
        val meta =
            pm
                .getApplicationInfo(context.packageName, PackageManager.GET_META_DATA)
                .metaData
        assertEquals(false, meta.getBoolean("firebase_messaging_auto_init_enabled", true))
        assertEquals(false, meta.getBoolean("delivery_metrics_exported_to_big_query_enabled", true))
    }

    @Test
    fun `disabling push on a phone that never enabled it is harmless`() {
        FirebasePushClient(context).disable()
        assertTrue(FirebaseApp.getApps(context).isEmpty())
        assertFalse(receiverEnabled("com.google.firebase.iid.FirebaseInstanceIdReceiver"))
    }
}
