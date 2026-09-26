package com.hypermind.jarvis.auth

import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.Instant
import java.util.UUID

class SessionManagerTest {
    private val server = MockWebServer().apply { start() }
    private val key = TestKey()
    private var now = Instant.parse("2026-09-26T12:00:00Z")
    private val sessions =
        SessionManager(
            api = { ApiClient(OkHttpClient(), server.url("/").toString()) },
            key = { key },
            deviceId = { UUID.fromString("6f1c2d3e-4a5b-4c6d-8e7f-0123456789ab") },
            now = { now },
        )

    @After
    fun tearDown() = server.shutdown()

    private fun token(
        value: String,
        expires: String,
    ) = server.enqueue(MockResponse().setBody("""{"access_token":"$value","expires_at":"$expires"}"""))

    @Test
    fun `a token is re-minted with a fresh proof only when it nears expiry`() {
        token("first", "2026-09-26T12:15:00Z")
        assertEquals("first", sessions.accessToken().first)
        assertEquals("first", sessions.accessToken().first)
        assertEquals(1, server.requestCount)
        val proof = server.takeRequest().body.readUtf8()
        assertTrue(proof.contains("v1.6f1c2d3e-4a5b-4c6d-8e7f-0123456789ab."))

        now = Instant.parse("2026-09-26T12:14:30Z")
        token("second", "2026-09-26T12:29:30Z")
        assertEquals("second", sessions.accessToken().first)
    }

    @Test
    fun `a refused proof means the enrollment is gone`() {
        server.enqueue(MockResponse().setResponseCode(401).setBody("""{"error":{"code":"unauthenticated"}}"""))
        assertThrows(EnrollmentLost::class.java) { sessions.accessToken() }
    }

    @Test
    fun `a server outage is not mistaken for revocation`() {
        server.enqueue(MockResponse().setResponseCode(503).setBody("""{"error":{"code":"dependency_unavailable"}}"""))
        val error = assertThrows(ApiException::class.java) { sessions.accessToken() }
        assertEquals(503, error.status)
    }

    @Test
    fun `the token never appears in its own string form`() {
        val token = ApiClient.Token("secret-token", "2026-09-26T12:15:00Z")
        assertTrue("secret-token" !in token.toString())
    }
}
