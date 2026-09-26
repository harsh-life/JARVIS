package com.hypermind.jarvis.contract

import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * docs/23 §6/§9: the shared perception samples, run through this client's
 * result models and [PerceptionCheck]. The server runs the same file through
 * `parse_observation`; a sample one side accepts and the other rejects fails
 * here or there — so the device can only build results the server accepts.
 */
class PerceptionSampleTest {
    private val document = ContractJson.parseToJsonElement(Shared.read("perception_samples.json")).jsonObject
    private val mapping = DeviceMapping.load(Shared.read("device_mapping.json"))

    @Test
    fun `the samples are for this mapping`() {
        assertEquals(mapping.version, document.getValue("mapping_version").jsonPrimitive.content)
    }

    @Test
    fun `every sample gets the server's verdict`() {
        val samples = document.getValue("samples").jsonArray
        assertTrue(samples.size >= 30)
        val kinds = mutableMapOf<ResultKind, MutableSet<Boolean>>()
        val failures = mutableListOf<String>()
        for (element in samples) {
            val sample = element.jsonObject
            val name = sample.getValue("name").jsonPrimitive.content
            val spec =
                requireNotNull(
                    mapping.lookup(
                        sample.getValue("capability").jsonPrimitive.content,
                        sample.getValue("operation").jsonPrimitive.content,
                    ),
                ) { "$name: not in the mapping" }
            val level =
                sample["perception_level"]?.let {
                    ContractJson.decodeFromJsonElement(PerceptionLevel.serializer(), it)
                }
            val valid = sample.getValue("valid").jsonPrimitive.boolean
            val problem =
                PerceptionCheck.problem(
                    spec.result,
                    sample.getValue("result").jsonObject,
                    level,
                    sample.getValue("package_name").jsonPrimitive.contentOrNull,
                )
            kinds.getOrPut(spec.result) { mutableSetOf() }.add(valid)
            if ((problem == null) != valid) failures += "$name: expected valid=$valid, got $problem"
        }
        assertEquals(emptyList<String>(), failures)
        assertEquals(ResultKind.entries.associateWith { setOf(true, false) }, kinds)
    }

    @Test
    fun `a password node cannot even be built with its text`() {
        val built =
            runCatching {
                ScreenNode(
                    id = 0,
                    role = "android.widget.EditText",
                    text = "hunter2",
                    bounds = listOf(0, 0, 1, 1),
                    password = true,
                )
            }
        assertTrue(built.isFailure)
    }

    @Test
    fun `a typed result round-trips through the envelope object`() {
        val result =
            ScreenReadResult(
                app = AppMetadata("com.example.notes"),
                nodes = listOf(ScreenNode(id = 0, role = "r", bounds = listOf(0, 0, 1, 1), password = true)),
            )
        val json = resultObject(result)
        assertEquals(
            null,
            PerceptionCheck.problem(ResultKind.SCREEN_READ, json, PerceptionLevel.ACCESSIBILITY, "com.example.notes"),
        )
        assertEquals(result, ContractJson.decodeFromJsonElement(ScreenReadResult.serializer(), json))
    }
}
