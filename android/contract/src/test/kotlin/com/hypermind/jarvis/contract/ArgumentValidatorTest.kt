package com.hypermind.jarvis.contract

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Test

class ArgumentValidatorTest {
    private val mapping = DeviceMapping.load(Shared.read("device_mapping.json"))
    private val tap = mapping.lookup("app.interact", "tap")!!

    private fun args(json: String): JsonObject = ContractJson.parseToJsonElement(json).jsonObject

    private fun ok(
        spec: PrimitiveSpec,
        json: String,
    ) = ArgumentValidator.problem(spec, args(json)) == null

    @Test
    fun `tap targets are named exactly one way`() {
        val cases =
            mapOf(
                """{"view_id":"send"}""" to true,
                """{"text":"Send","index":2}""" to true,
                """{"content_description":"Send"}""" to true,
                """{}""" to false,
                """{"view_id":"send","text":"Send"}""" to false,
                """{"view_id":""}""" to false,
                """{"view_id":7}""" to false,
                """{"view_id":"send","index":-1}""" to false,
                """{"view_id":"send","index":51}""" to false,
                """{"view_id":"send","index":true}""" to false,
                """{"view_id":"send","index":1.0}""" to false,
                """{"view_id":"send","index":"1"}""" to false,
                """{"view_id":"send","x":1}""" to false,
                """{"view_id":{"nested":1}}""" to false,
            )
        for ((json, expected) in cases) assertEquals(json, expected, ok(tap, json))
    }

    @Test
    fun `length is counted in code points like the server does`() {
        val emoji = "😀".repeat(200) // 200 code points, 400 UTF-16 units
        assertEquals(true, ok(tap, """{"view_id":"$emoji"}"""))
    }

    @Test
    fun `enums are closed`() {
        val global = mapping.lookup("device.ui_control", "global_action")!!
        assertEquals(true, ok(global, """{"action":"back"}"""))
        assertEquals(false, ok(global, """{"action":"power_off"}"""))
        assertEquals(false, ok(global, """{}"""))
    }

    @Test
    fun `a typed shizuku primitive accepts no arguments at all`() {
        val forceStop = mapping.lookup("app.interact", "force_stop")!!
        assertEquals(true, ok(forceStop, """{}"""))
        assertEquals(false, ok(forceStop, """{"argv":["am","force-stop","x"]}"""))
    }
}
