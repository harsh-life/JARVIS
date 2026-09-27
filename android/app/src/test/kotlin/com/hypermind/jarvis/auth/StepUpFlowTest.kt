package com.hypermind.jarvis.auth

import com.hypermind.jarvis.contract.DeviceProof
import com.hypermind.jarvis.contract.StepUp
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.util.UUID

/** docs/23 §3 step-up, device side, against a mock server. */
class StepUpFlowTest {
    private val server = MockWebServer().apply { start() }
    private val device = UUID.fromString("6f1c2d3e-4a5b-4c6d-8e7f-0123456789ab")
    private val pair: KeyPair =
        KeyPairGenerator
            .getInstance("EC")
            .apply {
                initialize(ECGenParameterSpec("secp256r1"))
            }.generateKeyPair()

    @After
    fun tearDown() = server.shutdown()

    private class SoftwareKeys(
        private val pair: KeyPair?,
        private val unavailable: Boolean = false,
    ) : StepUpKeyStore {
        var destroyed = false

        override fun create(): String {
            if (unavailable) throw StepUpUnavailable("no lock screen")
            return DeviceProof.b64(pair!!.public.encoded)
        }

        override fun signer(): Signature? =
            pair?.let {
                Signature.getInstance("SHA256withECDSA").apply { initSign(it.private) }
            }

        override fun destroy() {
            destroyed = true
        }
    }

    private fun flow(keys: StepUpKeyStore) =
        StepUpFlow(
            api = { ApiClient(OkHttpClient(), server.url("/").toString().trimEnd('/')) },
            accessToken = { "token-1" },
            keys = keys,
            deviceId = { device },
        )

    @Test
    fun `enrollment registers only the public half`() {
        server.enqueue(MockResponse().setResponseCode(204))
        assertTrue(flow(SoftwareKeys(pair)).enroll())
        val request = server.takeRequest()
        assertEquals("/api/v1/devices/me/step-up-key", request.path)
        assertEquals("Bearer token-1", request.getHeader("Authorization"))
        val sent = Json.parseToJsonElement(request.body.readUtf8()).jsonObject
        assertEquals(setOf("public_key"), sent.keys)
        assertEquals(DeviceProof.b64(pair.public.encoded), sent.getValue("public_key").jsonPrimitive.content)
    }

    @Test
    fun `a device that cannot hold the key enrolls without step-up`() {
        assertEquals(false, flow(SoftwareKeys(pair, unavailable = true)).enroll())
        assertEquals(0, server.requestCount)
    }

    @Test
    fun `the challenge is signed only after the user is present, and the signature verifies`() =
        runBlocking {
            server.enqueue(MockResponse().setBody("""{"challenge":"abc123","expires_at":"2026-09-27T07:00:00Z"}"""))
            server.enqueue(MockResponse().setBody("""{"reattested_until":"2026-09-27T07:05:00Z"}"""))
            var asked = 0
            val result =
                flow(SoftwareKeys(pair)).reattest { signer ->
                    asked++
                    signer
                }
            assertTrue(result is StepUpResult.Attested)
            assertEquals(1, asked)
            assertEquals("/api/v1/sessions/step-up/challenge", server.takeRequest().path)
            val attest = server.takeRequest()
            assertEquals("/api/v1/sessions/step-up", attest.path)
            val sent = Json.parseToJsonElement(attest.body.readUtf8()).jsonObject
            assertEquals("abc123", sent.getValue("challenge").jsonPrimitive.content)
            val verifier =
                Signature.getInstance("SHA256withECDSA").apply {
                    initVerify(pair.public)
                    update(StepUp.message(device.toString(), "abc123"))
                }
            assertTrue(verifier.verify(DeviceProof.unb64(sent.getValue("signature").jsonPrimitive.content)))
        }

    @Test
    fun `a user who does not authenticate sends nothing back`() =
        runBlocking {
            server.enqueue(MockResponse().setBody("""{"challenge":"abc123","expires_at":"2026-09-27T07:00:00Z"}"""))
            val result = flow(SoftwareKeys(pair)).reattest { null }
            assertEquals(StepUpResult.Cancelled, result)
            assertEquals(1, server.requestCount) // the challenge only; no attestation
        }

    @Test
    fun `without a usable key nothing is asked or sent`() =
        runBlocking {
            var asked = false
            val result =
                flow(SoftwareKeys(null)).reattest {
                    asked = true
                    it
                }
            assertEquals(StepUpResult.NoKey, result)
            assertEquals(false, asked)
            assertEquals(0, server.requestCount)
        }
}
