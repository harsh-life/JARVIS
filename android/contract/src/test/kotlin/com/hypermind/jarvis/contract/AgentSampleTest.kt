package com.hypermind.jarvis.contract

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * docs/29 §23.3 (Phase 4): the owner's agents and agent runs as the server
 * serializes them (`shared/android/agent_samples.json`), parsed strictly. A run
 * is shown as the ordinary task it ran as; nothing here decides anything.
 */
class AgentSampleTest {
    private val samples = ContractJson.parseToJsonElement(Shared.read("agent_samples.json")).jsonObject

    private fun body(name: String) = samples.getValue(name).toString()

    @Test
    fun `the owner's agents parse, and only an active one offers a run`() {
        val shown = AgentList.parse(200, body("agent_list")) as AgentList.Shown
        assertEquals(listOf("Security advisory digest", "Weekly reading list"), shown.agents.map { it.name })
        assertTrue(shown.agents[0].runnable)
        assertFalse(shown.agents[1].runnable)
    }

    @Test
    fun `a completed run is the task it ran as`() {
        val view = AgentRunResponse.parse(202, body("run_completed")) as TaskView.Result
        assertEquals(TaskStatus.COMPLETED, view.result.status)
        assertEquals("Two critical advisories this morning.", view.result.response)
    }

    @Test
    fun `a run waiting for the owner shows the server's own pending action`() {
        val view = AgentRunResponse.parse(202, body("run_waiting")) as TaskView.Result
        assertEquals(TaskStatus.AWAITING_CONFIRMATION, view.result.status)
        assertEquals("file.read", view.result.pending?.capability)
        assertEquals("ct_sample_token_not_real", view.result.pending?.confirmationToken)
    }

    @Test
    fun `a refused run says why the server refused it`() {
        val view = AgentRunResponse.parse(409, body("run_refused")) as TaskView.Failed
        assertEquals("paused", view.code)
    }

    @Test
    fun `a run response carrying anything unexpected is malformed, never guessed`() {
        for (field in listOf("authorized", "grant", "approved")) {
            val tampered = JsonObject(samples.getValue("run_completed").jsonObject + (field to JsonPrimitive(true)))
            val view = AgentRunResponse.parse(202, tampered.toString()) as TaskView.Failed
            assertEquals(field, "malformed_response", view.code)
        }
        assertEquals("malformed_response", (AgentList.parse(200, "{}") as AgentList.Failed).code)
    }

    @Test
    fun `the tap request is exactly what the server parses, and an on-demand run names nothing`() {
        val tap = RunAgentRequest.encode(reminderDeliveryId = "6f1c2d3e-4a5b-4c6d-8e7f-00000000e001")
        assertEquals(samples.getValue("run_tap_request"), ContractJson.parseToJsonElement(tap))
        assertEquals("{}", RunAgentRequest.encode(reminderDeliveryId = null))
    }

    @Test
    fun `an agent's name never reaches a log line through toString`() {
        val shown = AgentList.parse(200, body("agent_list")) as AgentList.Shown
        assertFalse("advisory" in shown.agents[0].toString())
        assertFalse("advisory" in shown.toString())
    }
}
