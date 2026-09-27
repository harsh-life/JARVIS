package com.hypermind.jarvis.contract

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** ANDC-T9 on the device: only the exact wake is acted on (docs/23 §4). */
class PushSampleTest {
    private val doc = ContractJson.parseToJsonElement(Shared.read("push_samples.json")).jsonObject

    private fun JsonObject.asStringMap(): Map<String, String> = mapValues { it.value.jsonPrimitive.content }

    @Test
    fun `the server's wake is the one data map the phone acts on`() {
        assertTrue(WakeSignal.isWake(doc.getValue("wake_data").jsonObject.asStringMap()))
        val message =
            doc
                .getValue("fcm_message")
                .jsonObject
                .getValue("message")
                .jsonObject
        assertTrue(WakeSignal.isWake(message.getValue("data").jsonObject.asStringMap()))
        // A data message only: nothing for the system to render.
        assertFalse("notification" in message)
    }

    @Test
    fun `anything else, including the wake with anything added, is ignored`() {
        val ignored = doc.getValue("ignored_data").jsonArray
        assertTrue(ignored.size >= 8)
        ignored.forEach { assertFalse(it.toString(), WakeSignal.isWake(it.jsonObject.asStringMap())) }
    }

    @Test
    fun `the registration names only the provider and this device's own token`() {
        val sample = doc.getValue("registration").jsonObject
        val token = sample.getValue("token").jsonPrimitive.content
        val encoded =
            ContractJson.encodeToString(
                PushTokenRegistration.serializer(),
                PushTokenRegistration(token = token),
            )
        assertEquals(sample, ContractJson.parseToJsonElement(encoded))
        assertFalse(PushTokenRegistration(token = token).toString().contains(token))
        assertTrue(runCatching { PushTokenRegistration(token = "short") }.isFailure)
        assertTrue(runCatching { PushTokenRegistration(token = "$token\n") }.isFailure)
    }

    @Test
    fun `push config parses strictly, and none carries no options`() {
        val none = ContractJson.decodeFromJsonElement(PushClientConfig.serializer(), doc.getValue("config_none"))
        assertEquals(PushProviderKind.NONE, none.provider)
        assertNull(none.fcm)
        val fcm = ContractJson.decodeFromJsonElement(PushClientConfig.serializer(), doc.getValue("config_fcm"))
        assertEquals("jarvis-sample-project", fcm.fcm?.projectId)
        val extra =
            doc
                .getValue(
                    "config_fcm",
                ).toString()
                .replace("\"provider\"", "\"service_account\":\"x\",\"provider\"")
        assertTrue(runCatching { ContractJson.decodeFromString(PushClientConfig.serializer(), extra) }.isFailure)
        val mismatched = "{\"provider\":\"fcm\"}"
        assertTrue(runCatching { ContractJson.decodeFromString(PushClientConfig.serializer(), mismatched) }.isFailure)
    }
}
