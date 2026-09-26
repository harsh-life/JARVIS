package com.hypermind.jarvis.auth

import android.content.Context
import android.net.Uri
import androidx.test.core.app.ApplicationProvider
import com.hypermind.jarvis.contract.DeviceProof
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.security.MessageDigest

@RunWith(RobolectricTestRunner::class)
class LoginCoordinatorTest {
    private val server = MockWebServer()
    private lateinit var store: EnrollmentStore
    private val keys = TestKeyStore()
    private var now = 1_000_000L
    private lateinit var coordinator: LoginCoordinator

    @Before
    fun setUp() {
        server.start()
        val prefs =
            ApplicationProvider
                .getApplicationContext<Context>()
                .getSharedPreferences("test-${System.nanoTime()}", Context.MODE_PRIVATE)
        store = EnrollmentStore(prefs)
        val api = { ApiClient(OkHttpClient(), server.url("/").toString()) }
        val sessions = SessionManager(api, { keys.key }, { store.deviceId })
        coordinator = LoginCoordinator(store, keys, api, sessions, nowMillis = { now })
    }

    @After
    fun tearDown() = server.shutdown()

    private fun begin(): String {
        server.enqueue(MockResponse().setBody("""{"redirect_url":"https://accounts.example/o?x=1"}"""))
        coordinator.begin()
        return server.takeRequest().requestUrl!!.queryParameter("app_state")!!
    }

    private fun link(
        token: String,
        state: String,
    ) = Uri.parse("https://jarvis.example/app/login#bootstrap_token=$token&app_state=$state")

    @Test
    fun `the app's own login enrolls with a device-held key and a proof of possession`() {
        val state = begin()
        assertTrue(state.length >= 32)
        server.enqueue(
            MockResponse().setResponseCode(201).setBody("""{"device_id":"6f1c2d3e-4a5b-4c6d-8e7f-0123456789ab"}"""),
        )
        server.enqueue(MockResponse().setBody("""{"access_token":"t","expires_at":"2099-01-01T00:00:00Z"}"""))

        val outcome = coordinator.complete(link("boot-token", state))

        assertTrue(outcome is LoginOutcome.Enrolled)
        val register = server.takeRequest()
        assertEquals("Bearer boot-token", register.getHeader("Authorization"))
        val body = Json.parseToJsonElement(register.body.readUtf8()).jsonObject
        assertEquals(setOf("platform", "public_key", "key_proof"), body.keys)
        val publicKey = body.getValue("public_key").jsonPrimitive.content
        assertEquals(DeviceProof.b64(keys.key!!.publicKey), publicKey)
        // The signature is over the server's registration message for this token.
        val digest =
            MessageDigest.getInstance("SHA-256").digest("boot-token".toByteArray()).joinToString("") {
                "%02x".format(it)
            }
        keys.key!!.verify(
            DeviceProof.unb64(body.getValue("key_proof").jsonPrimitive.content),
            "hypermind-device-register|v1|$digest|$publicKey".toByteArray(),
        )
        assertEquals("6f1c2d3e-4a5b-4c6d-8e7f-0123456789ab", store.deviceId.toString())
    }

    @Test
    fun `a link from a login this app did not start is ignored`() {
        val outcome = coordinator.complete(link("boot", "x".repeat(43)))
        assertEquals(LoginOutcome.Rejected(LoginRejection.NO_PENDING_LOGIN), outcome)
        assertNull(keys.key)
        assertEquals(0, server.requestCount)
    }

    @Test
    fun `a link carrying another login's nonce is ignored and the pending one is spent`() {
        begin()
        val outcome = coordinator.complete(link("attacker-boot", "y".repeat(43)))
        assertEquals(LoginOutcome.Rejected(LoginRejection.STATE_MISMATCH), outcome)
        assertNull(keys.key)
        assertEquals(
            LoginOutcome.Rejected(LoginRejection.NO_PENDING_LOGIN),
            coordinator.complete(link("b", "y".repeat(43))),
        )
    }

    @Test
    fun `a stale pending login is refused`() {
        val state = begin()
        now += 11 * 60 * 1000
        assertEquals(LoginOutcome.Rejected(LoginRejection.EXPIRED), coordinator.complete(link("boot", state)))
    }

    @Test
    fun `a malformed link is refused without spending the pending login`() {
        val state = begin()
        assertEquals(
            LoginOutcome.Rejected(LoginRejection.MALFORMED_LINK),
            coordinator.complete(Uri.parse("https://jarvis.example/app/login")),
        )
        server.enqueue(
            MockResponse().setResponseCode(201).setBody("""{"device_id":"6f1c2d3e-4a5b-4c6d-8e7f-0123456789ab"}"""),
        )
        server.enqueue(MockResponse().setBody("""{"access_token":"t","expires_at":"2099-01-01T00:00:00Z"}"""))
        assertTrue(coordinator.complete(link("boot", state)) is LoginOutcome.Enrolled)
    }
}
