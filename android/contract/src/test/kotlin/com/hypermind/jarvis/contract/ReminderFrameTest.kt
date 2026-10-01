package com.hypermind.jarvis.contract

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * docs/22 §2: the reminder frames the server serializes
 * (`shared/android/reminder_samples.json`), parsed strictly. A reminder is a
 * message; any field that would make it an instruction is a parse failure.
 */
class ReminderFrameTest {
    private val samples = ContractJson.parseToJsonElement(Shared.read("reminder_samples.json")).jsonObject
    private val serverSide = samples.getValue("server_to_device").jsonObject
    private val deviceSide = samples.getValue("device_to_server").jsonObject

    private fun frame(name: String) = serverSide.getValue(name).toString()

    @Test
    fun `the server's reminders parse`() {
        val onTime = ServerFrame.parse(frame("on_time")) as ServerFrame.ReminderNotice
        assertEquals("Call the dentist to move Thursday's appointment", onTime.reminder.taskReason)
        assertFalse(onTime.reminder.late)
        val late = ServerFrame.parse(frame("late_recurring")) as ServerFrame.ReminderNotice
        assertTrue(late.reminder.late && late.reminder.recurring)
    }

    @Test
    fun `a reminder carrying an operation or authority field is invalid`() {
        for (field in listOf("capability", "operation", "primitive", "arguments", "confirmation_token", "authorized")) {
            val tampered = JsonObject(serverSide.getValue("on_time").jsonObject + (field to JsonPrimitive("x")))
            assertTrue(field, ServerFrame.parse(tampered.toString()) is ServerFrame.Invalid)
        }
    }

    @Test
    fun `an empty reminder is invalid`() {
        val empty = JsonObject(serverSide.getValue("on_time").jsonObject + ("task_reason" to JsonPrimitive(" ")))
        assertTrue(ServerFrame.parse(empty.toString()) is ServerFrame.Invalid)
    }

    @Test
    fun `the device's frames are exactly what the server parses`() {
        val ack = ReminderAck(deliveryId = "6f1c2d3e-4a5b-4c6d-8e7f-00000000e001").encode()
        assertEquals(deviceSide.getValue("ack"), ContractJson.parseToJsonElement(ack))
        val hello =
            Hello(
                accessToken = "sample-token-not-real",
                deviceProof = "v1.sample.proof.not.real",
                mappingVersion = "1-0123456789abcdef",
                clientVersion = "0.1",
                features = listOf(ChannelFeature.REMINDERS),
            ).encode()
        assertEquals(deviceSide.getValue("hello_with_reminders"), ContractJson.parseToJsonElement(hello))
    }

    @Test
    fun `an agent reminder carries its agent as an identifier, and a plain one none`() {
        val agent = ServerFrame.parse(frame("agent_reminder")) as ServerFrame.ReminderNotice
        assertEquals("6f1c2d3e-4a5b-4c6d-8e7f-00000000a001", agent.reminder.agentId)
        val plain = ServerFrame.parse(frame("on_time")) as ServerFrame.ReminderNotice
        assertEquals(null, plain.reminder.agentId)
        val malformed =
            JsonObject(serverSide.getValue("agent_reminder").jsonObject + ("agent_id" to JsonPrimitive("x")))
        assertTrue(ServerFrame.parse(malformed.toString()) is ServerFrame.Invalid)
    }

    @Test
    fun `this client declares agent reminders exactly as the server parses them`() {
        val hello =
            Hello(
                accessToken = "sample-token-not-real",
                deviceProof = "v1.sample.proof.not.real",
                mappingVersion = "1-0123456789abcdef",
                clientVersion = "0.1",
                features = listOf(ChannelFeature.REMINDERS, ChannelFeature.AGENT_REMINDERS),
            ).encode()
        assertEquals(deviceSide.getValue("hello_with_agent_reminders"), ContractJson.parseToJsonElement(hello))
    }

    @Test
    fun `the user's words never reach a log line through toString`() {
        val reminder = (ServerFrame.parse(frame("on_time")) as ServerFrame.ReminderNotice).reminder
        assertFalse("dentist" in reminder.toString())
    }
}
