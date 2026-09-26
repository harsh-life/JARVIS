package com.hypermind.jarvis.contract

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class DeviceMappingTest {
    private val raw = Shared.read("device_mapping.json")

    @Test
    fun `the shared table loads and its version is its own digest`() {
        val mapping = DeviceMapping.load(raw)
        assertTrue(mapping.version.matches(Regex("1-[0-9a-f]{16}")))
    }

    @Test
    fun `canonical json reproduces the server's python serialization byte for byte`() {
        // Input and expected output are both plain (escaped) literals: a raw
        // string would let the Kotlin compiler decode the \u sequences first.
        val doc =
            ContractJson.parseToJsonElement(
                "{\"b\":\"\\u00e9\\\"\\\\\\n\\t\\u0001/x\",\"a\":[1,-2,true,null,{\"z\":0,\"y\":\"\\ud83d\\ude00\"}]}",
            )
        val expected =
            "{\"a\":[1,-2,true,null,{\"y\":\"\\ud83d\\ude00\",\"z\":0}],\"b\":\"\\u00e9\\\"\\\\\\n\\t\\u0001/x\"}"
        val encoded = CanonicalJson.encode(doc)
        assertEquals(expected, encoded)
        assertEquals(
            "5a25f366e563f7c2a20e91a69f028a8a096c76b126ee13798277e49a6f07da87",
            CanonicalJson.sha256Hex(encoded),
        )
    }

    @Test
    fun `a table whose content does not match its version is refused`() {
        val root = ContractJson.parseToJsonElement(raw).jsonObject
        val tampered = raw.replace("\"max_length\": 2000", "\"max_length\": 20000")
        assertTrue(tampered != raw)
        assertThrows(MappingIntegrityError::class.java) { DeviceMapping.load(tampered) }
        val relabelled =
            JsonObject(root + ("mapping_version" to JsonPrimitive("1-0000000000000000")))
        assertThrows(MappingIntegrityError::class.java) { DeviceMapping.load(relabelled.toString()) }
    }

    @Test
    fun `an unknown field in the table is a parse failure not an ignored key`() {
        val injected = raw.replaceFirst("\"schema\": 1", "\"schema\": 1, \"elevated_shell\": true")
        assertThrows(Exception::class.java) { DeviceMapping.load(injected) }
    }

    @Test
    fun `the table exposes no system_restricted and no shell primitive`() {
        val mapping = DeviceMapping.load(raw)
        assertEquals(setOf("app.interact", "device.read", "device.ui_control"), mapping.capabilityNames)
        assertNull(mapping.lookup("system.restricted", "run_shell_command"))
        val shizuku =
            mapping.capabilityNames.flatMap { cap ->
                mapping.operations(cap).mapNotNull { op ->
                    mapping.lookup(cap, op)?.takeIf { it.mechanism == Mechanism.SHIZUKU }?.let { "$cap.$op" }
                }
            }
        assertEquals(listOf("app.interact.force_stop"), shizuku)
        assertTrue(mapping.lookup("app.interact", "force_stop")!!.arguments.isEmpty())
    }

    @Test
    fun `screenshot is a separate operation with its own toggle`() {
        val mapping = DeviceMapping.load(raw)
        val shot = mapping.lookup("device.read", "capture_screenshot")!!
        assertEquals(GridToggle.SCREENSHOT, shot.gridToggle)
        assertTrue(PlatformDependency.SCREEN_CAPTURE in shot.dependencies)
        val read = mapping.lookup("device.read", "read_screen")!!
        assertTrue(PlatformDependency.SCREEN_CAPTURE !in read.dependencies)
    }
}
