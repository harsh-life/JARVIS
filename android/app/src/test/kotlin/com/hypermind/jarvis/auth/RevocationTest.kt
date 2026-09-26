package com.hypermind.jarvis.auth

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
class RevocationTest {
    @Test
    fun `revocation forgets the key, the enrollment and every local grant`() {
        val prefs = ApplicationProvider.getApplicationContext<Context>().getSharedPreferences("r", Context.MODE_PRIVATE)
        val store = EnrollmentStore(prefs).apply { serverUrl = "https://jarvis.example" }
        store.enrolled(UUID.randomUUID())
        val keys = TestKeyStore().apply { create() }
        val sessions = SessionManager({ error("no network in this test") }, { keys.key }, { store.deviceId })
        val revocation = Revocation(keys, store, sessions)
        var gridWiped = false
        revocation.onWipe { gridWiped = true }

        revocation.wipe()

        assertNull(keys.key)
        assertEquals(1, keys.destroyed)
        assertNull(store.deviceId)
        assertEquals(true, gridWiped)
        assertEquals("https://jarvis.example", store.serverUrl)
    }

    @Test
    fun `only a bare https origin is accepted as a server`() {
        assertEquals("https://jarvis.example", ApiClient.validServerUrl("https://jarvis.example/"))
        assertEquals("https://jarvis.example:8443", ApiClient.validServerUrl("https://jarvis.example:8443"))
        assertNull(ApiClient.validServerUrl("http://jarvis.example"))
        assertNull(ApiClient.validServerUrl("https://jarvis.example/path"))
        assertNull(ApiClient.validServerUrl("jarvis.example"))
    }
}
