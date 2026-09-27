package com.hypermind.jarvis.push

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import com.hypermind.jarvis.auth.ApiClient
import com.hypermind.jarvis.contract.ContractJson
import com.hypermind.jarvis.contract.FcmClientOptions
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.serialization.json.jsonObject
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.SocketPolicy
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.io.File

/** Push registration follows the server's offer and the user's choice — off by default (docs/23 §4). */
@RunWith(RobolectricTestRunner::class)
class PushRegistrarTest {
    private val context = ApplicationProvider.getApplicationContext<Context>()
    private val server = MockWebServer().apply { start() }
    private val samples =
        ContractJson
            .parseToJsonElement(
                File(requireNotNull(System.getProperty("jarvis.shared.dir"))).resolve("push_samples.json").readText(),
            ).jsonObject
    private val token = "rotating-token-" + "0123456789abcdef".repeat(4)
    private val rotated = "rotated-token-" + "fedcba9876543210".repeat(4)

    private class FakeClient : PushClient {
        val enabled = mutableListOf<FcmClientOptions>()
        var disabled = 0
        var deliver: String? = null
        var unavailable: String? = null

        override fun enable(
            options: FcmClientOptions,
            onToken: (String) -> Unit,
            onUnavailable: (String) -> Unit,
        ) {
            enabled += options
            unavailable?.let(onUnavailable) ?: deliver?.let(onToken)
        }

        override fun disable() {
            disabled++
        }
    }

    private val client = FakeClient()
    private val settings = PushSettings(context.getSharedPreferences("push-${System.nanoTime()}", Context.MODE_PRIVATE))
    private var enrolled = true

    private val registrar =
        PushRegistrar(
            api = { ApiClient(OkHttpClient(), server.url("/").toString().trimEnd('/')) },
            accessToken = { "token-1" },
            enrolled = { enrolled },
            settings = settings,
            client = client,
            scope = CoroutineScope(Dispatchers.Unconfined),
        )

    @After
    fun tearDown() = server.shutdown()

    private fun config(name: String) = server.enqueue(MockResponse().setBody(samples.getValue(name).toString()))

    private fun noContent() = server.enqueue(MockResponse().setResponseCode(204))

    @Test
    fun `by default nothing push-related runs`() {
        assertFalse(settings.optedIn)
        config("config_fcm")
        assertEquals(PushRegistrar.Status.Off, registrar.sync())
        assertTrue(client.enabled.isEmpty())
        assertEquals(1, server.requestCount) // the config GET only; no token sent
    }

    @Test
    fun `a server without push never starts the SDK, even if the user opted in`() {
        settings.optedIn = true
        config("config_none")
        assertEquals(PushRegistrar.Status.Off, registrar.sync())
        assertTrue(client.enabled.isEmpty())
    }

    @Test
    fun `an un-enrolled phone does nothing at all`() {
        enrolled = false
        settings.optedIn = true
        assertEquals(PushRegistrar.Status.Off, registrar.sync())
        assertEquals(0, server.requestCount)
        assertTrue(client.enabled.isEmpty())
    }

    @Test
    fun `opting in binds this device's token with nothing else in the body, once`() {
        client.deliver = token
        config("config_fcm")
        noContent()
        assertEquals(PushRegistrar.Status.Enabling, registrar.setOptedIn(true))
        assertEquals("jarvis-sample-project", client.enabled.single().projectId)
        server.takeRequest() // config
        val put = server.takeRequest()
        assertEquals("PUT", put.method)
        assertEquals("/api/v1/devices/me/push-token", put.path)
        assertEquals("Bearer token-1", put.getHeader("Authorization"))
        assertEquals("""{"provider":"fcm","token":"$token"}""", put.body.readUtf8())
        // The same token again (e.g. every connect): not re-sent.
        registrar.register(token)
        assertEquals(2, server.requestCount)
    }

    @Test
    fun `a rotated token is re-bound`() {
        client.deliver = token
        config("config_fcm")
        noContent()
        registrar.setOptedIn(true)
        noContent()
        registrar.register(rotated)
        server.takeRequest()
        server.takeRequest()
        val second = server.takeRequest()
        assertTrue(second.body.readUtf8().contains(rotated))
    }

    @Test
    fun `opting out stops the SDK and tells the server to forget the token`() {
        client.deliver = token
        config("config_fcm")
        noContent()
        registrar.setOptedIn(true)
        config("config_fcm")
        noContent()
        assertEquals(PushRegistrar.Status.Off, registrar.setOptedIn(false))
        assertEquals(1, client.disabled)
        repeat(3) { server.takeRequest() }
        assertEquals("DELETE", server.takeRequest().method)
    }

    @Test
    fun `an unreachable server or a refusal leaves it pending, retried on the next connect`() {
        settings.optedIn = true
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AT_START))
        assertTrue(registrar.sync() is PushRegistrar.Status.Pending)
        client.deliver = token
        config("config_fcm")
        server.enqueue(MockResponse().setResponseCode(409).setBody("""{"error":{"code":"conflict"}}"""))
        registrar.sync()
        assertTrue(registrar.status is PushRegistrar.Status.Pending)
        assertFalse(settings.isRegistered(token))
    }

    @Test
    fun `a push config that does not parse strictly enables nothing`() {
        settings.optedIn = true
        val tampered = samples.getValue("config_fcm").toString().replace("\"provider\"", "\"extra\":1,\"provider\"")
        server.enqueue(MockResponse().setBody(tampered))
        assertTrue(registrar.sync() is PushRegistrar.Status.Pending)
        assertTrue(client.enabled.isEmpty())
    }

    @Test
    fun `a phone without push support reports it and works without`() {
        settings.optedIn = true
        client.unavailable = "no Google Play services"
        config("config_fcm")
        registrar.sync()
        assertEquals(PushRegistrar.Status.Unavailable("no Google Play services"), registrar.status)
        assertEquals(1, server.requestCount)
    }

    @Test
    fun `revocation stops the SDK and forgets everything`() {
        client.deliver = token
        config("config_fcm")
        noContent()
        registrar.setOptedIn(true)
        registrar.wipe()
        assertEquals(1, client.disabled)
        assertFalse(settings.optedIn)
        assertFalse(settings.hasRegistration())
    }
}
