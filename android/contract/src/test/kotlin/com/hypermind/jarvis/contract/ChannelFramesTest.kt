package com.hypermind.jarvis.contract

import org.junit.Assert.assertTrue
import org.junit.Test

class ChannelFramesTest {
    private val op =
        """{"type":"operation","op_id":"6f1c2d3e-4a5b-4c6d-8e7f-0123456789ab",""" +
            """"task_id":"6f1c2d3e-4a5b-4c6d-8e7f-0123456789ac","device_id":"6f1c2d3e-4a5b-4c6d-8e7f-0123456789ad",""" +
            """"capability":"device.read","operation":"read_battery","primitive":"android.api.battery_state",""" +
            """"arguments":{},"mapping_version":"1-0123456789abcdef",""" +
            """"issued_at":"2026-09-26T12:00:00Z","expires_at":"2026-09-26T12:00:30Z"}"""

    @Test
    fun `an operation parses`() {
        assertTrue(ServerFrame.parse(op) is ServerFrame.Operation)
    }

    @Test
    fun `an operation carrying an authority field is rejected`() {
        val withGrant = op.replace("\"arguments\":{}", "\"arguments\":{},\"authorized\":true")
        assertTrue(ServerFrame.parse(withGrant) is ServerFrame.Invalid)
    }

    @Test
    fun `unknown and malformed frames are invalid`() {
        assertTrue(ServerFrame.parse("""{"type":"grant","capability":"x"}""") is ServerFrame.Invalid)
        assertTrue(ServerFrame.parse("not json") is ServerFrame.Invalid)
        assertTrue(ServerFrame.parse("[]") is ServerFrame.Invalid)
    }

    @Test
    fun `a cancel names exactly one target`() {
        assertTrue(ServerFrame.parse("""{"type":"cancel","op_id":"a"}""") is ServerFrame.Cancel)
        assertTrue(ServerFrame.parse("""{"type":"cancel"}""") is ServerFrame.Invalid)
        assertTrue(ServerFrame.parse("""{"type":"cancel","op_id":"a","task_id":"b"}""") is ServerFrame.Invalid)
    }

    @Test
    fun `an oversized frame is refused before parsing`() {
        assertTrue(ServerFrame.parse("x".repeat(Bounds.MAX_RESULT_FRAME_BYTES + 1)) is ServerFrame.Invalid)
    }

    @Test
    fun `credentials never appear in a frame's toString`() {
        val hello =
            Hello(
                accessToken = "secret-token-value",
                deviceProof = "v1.x.y.z.sig",
                mappingVersion = "1-a",
                clientVersion = "0.1",
            )
        assertTrue("secret-token-value" !in hello.toString())
        assertTrue("sig" !in hello.toString())
    }
}
