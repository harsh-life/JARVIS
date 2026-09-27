package com.hypermind.jarvis.contract

import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** docs/27: `GET /voice/config` as the server renders it, parsed strictly. */
class VoiceSampleTest {
    private val samples = ContractJson.parseToJsonElement(Shared.read("voice_samples.json")).jsonObject

    private fun view(name: String): VoiceConfigView =
        ContractJson.decodeFromString(VoiceConfigView.serializer(), samples.getValue(name).toString())

    @Test
    fun `every placement parses`() {
        assertEquals(VoicePlacement.DEVICE, view("config_default_device").stt)
        assertEquals(VoicePlacement.DEVICE, view("config_default_device").tts)
        assertEquals(VoicePlacement.SERVER, view("config_server_stt_tts_off").stt)
        assertEquals(VoicePlacement.OFF, view("config_server_stt_tts_off").tts)
        assertEquals(VoicePlacement.OFF, view("config_all_off").stt)
    }

    @Test
    fun `the client default matches the server default`() {
        assertEquals(VoiceConfigView.DEFAULT, view("config_default_device"))
    }

    @Test
    fun `a view naming a provider or endpoint is rejected`() {
        for (field in listOf("provider", "endpoint", "secret_ref")) {
            val tampered =
                JsonObject(
                    samples.getValue("config_default_device").jsonObject + (field to JsonPrimitive("x")),
                )
            val rejected =
                try {
                    ContractJson.decodeFromString(VoiceConfigView.serializer(), tampered.toString())
                    false
                } catch (ignored: SerializationException) {
                    true
                }
            assertTrue(field, rejected)
        }
    }
}
